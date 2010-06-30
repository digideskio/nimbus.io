# -*- coding: utf-8 -*-
"""
diyapi_anti_entropy_server.py

Performs weekly or monthly consistency checks on every avatar.
Query each machine for a "database consistency check hash" 
(see below) for the avatar.
Every machine on the network replies with it's consistency check hash for 
that avatar.
If consistency hashes match, done, move on to next avatar.
If consistency hashes don't match, schedule avatar for recheck in an hour.
For any avatar that misses 3 consistency checks in a row, 
do item level comparisons between nodes (see below.)

Avatar's "database consistency hash": 
Generated by querying the DB only on each machine for each avatar. 
A hash is constructed from the sorted keys, 
adding the key, and the timestamp from the value, 
and the md5 from the stored value (if we have data) 
or a marker for a tombstone if we have one of those.

Item level comparisons: Pull all 10 databases. Iterate through them. 
(since they are all sorted, this doesn't require unbounded memory.) 
Ignore keys stored in the last hour, which may still be settling. 
Based on the timestamp values present for each key, 
you should be able to determine the "correct" state. 
I.e. if a tombstone is present, it means any earlier keys should not be there. 
If only some (but not all) shares are there, the remaining shares should be 
reconstructed and added. 
Any other situation would indicate a data integrity error 
that should be resolved.
"""
from collections import namedtuple
import datetime
import logging
import os
import random
import sys
import time
import uuid

from diyapi_tools import message_driven_process as process
from diyapi_tools.amqp_connection import local_exchange_name 
from diyapi_tools.low_traffic_thread import LowTrafficThread, \
        low_traffic_routing_tag

from diyapi_anti_entropy_server.audit_result_database import \
    AuditResultDatabase 

from messages.anti_entropy_audit_request import AntiEntropyAuditRequest
from messages.anti_entropy_audit_reply import AntiEntropyAuditReply
from messages.database_avatar_list_request import DatabaseAvatarListRequest
from messages.database_avatar_list_reply import DatabaseAvatarListReply
from messages.database_consistency_check import DatabaseConsistencyCheck
from messages.database_consistency_check_reply import \
    DatabaseConsistencyCheckReply

_log_path = u"/var/log/pandora/diyapi_anti_entropy_server_%s.log" % (
    os.environ["SPIDEROAK_MULTI_NODE_NAME"],
)
_queue_name = "anti-entropy-%s" % (
    os.environ["SPIDEROAK_MULTI_NODE_NAME"], 
)
_routing_header = "anti-entropy"
_routing_key_binding = ".".join([_routing_header, "*"])
_database_avatar_list_reply_routing_key = ".".join([
    _routing_header,
    DatabaseAvatarListReply.routing_tag,
])
_database_consistency_check_reply_routing_key = ".".join([
    _routing_header,
    DatabaseConsistencyCheckReply.routing_tag,
])
_low_traffic_routing_key = ".".join([
    _routing_header, 
    low_traffic_routing_tag,
])
_polling_interval = float(os.environ.get(
    "DIYAPI_ANTI_ENTROPY_POLLING_INTERVAL", "1800.0")
)
_avatar_polling_interval = float(os.environ.get(
    "DIYAPI_ANTI_ENTROPY_AVATAR_POLLING_INTERVAL", "86400.0") # 24 * 60 * 60
)
_request_timeout = 5.0 * 60.0
_retry_interval = 60.0 * 60.0
_max_retry_count = 2
_exchanges = os.environ["DIY_NODE_EXCHANGES"].split()
_error_hash = "*** error ***"
_audit_cutoff_days = int(os.environ.get("DIYAPI_AUDIT_CUTOFF_DAYS", "14"))

_request_state_tuple = namedtuple("RequestState", [ 
    "timestamp",
    "timeout",
    "timeout_function",
    "avatar_id",
    "retry_count",
    "replies",
    "row_id",
    "reply_exchange",
    "reply_routing_header"
])
_retry_entry_tuple = namedtuple("RetryEntry", [
    "retry_time", 
    "avatar_id", 
    "row_id",
    "retry_count"
])

def _is_request_state((_, value, )):
    return value.__class__.__name__ == "RequestState"

def _create_state():
    return {
        "retry-list" : list(),
    }

def _next_poll_interval():
    return time.time() + _polling_interval

def _next_avatar_poll_interval():
    return time.time() + _avatar_polling_interval

def _retry_time():
    return time.time() + _retry_interval

def _request_avatar_ids():
    # request a list of avatar ids from the local database server
    request_id = uuid.uuid1().hex

    message = DatabaseAvatarListRequest(
        request_id,
        local_exchange_name,
        _routing_header
    )

    return [(local_exchange_name, message.routing_key, message, ), ]

def _timeout_request(request_id, state):
    """
    If we don't hear from all the nodes in a reasonable time,
    put the request in the retry queue
    """
    log = logging.getLogger("_timeout_request")
    try:
        request_state = state.pop(request_id)
    except KeyError:
        log.error("can't find %s in state" % (request_id, ))
        return

    database = AuditResultDatabase()

    if request_state.retry_count >= _max_retry_count:
        log.error("timeout: %s with too many retries %s " % (
            request_state.avatar_id, request_state.retry_count
        ))
        database.too_many_retries(request_state.row_id)
        database.close()
        # TODO: need to do something with this
        return

    log.error("timeout %s. will retry in %s seconds" % (
        request_state.avatar_id, _retry_interval,
    ))
    state["retry-list"].append(
        _retry_entry_tuple(
            retry_time=_retry_time(), 
            avatar_id=request_state.avatar_id,
            row_id=request_state.row_id,
            retry_count=request_state.retry_count, 
        )
    )
    database.wait_for_retry(request_state.row_id)
    database.close()

def _start_consistency_check(state, avatar_id, row_id=None, retry_count=0):
    log = logging.getLogger("_start_consistency_check")
    log.info("start consistency check on %s" % (avatar_id, ))

    request_id = uuid.uuid1().hex
    timestamp = datetime.datetime.now()

    database = AuditResultDatabase()
    if row_id is None:
        row_id = database.start_audit(avatar_id, timestamp)
    else:
        database.restart_audit(row_id, timestamp)
    database.close()

    state[request_id] = _request_state_tuple(
        timestamp=timestamp,
        timeout=time.time()+_request_timeout,
        timeout_function=_timeout_request,
        avatar_id=avatar_id,
        retry_count=retry_count,
        replies=dict(), 
        row_id=row_id,
        reply_exchange=None,
        reply_routing_header=None
    )

    message = DatabaseConsistencyCheck(
        request_id,
        avatar_id,
        time.mktime(timestamp.timetuple()),
        local_exchange_name,
        _routing_header
    )
    # send the DatabaseConsistencyCheck to every node
    return [
        (dest_exchange, message.routing_key, message) \
        for dest_exchange in _exchanges
    ]

def _choose_avatar_for_consistency_check(state):
    """pick an avatar and start a new consistency check"""
    log = logging.getLogger("_choose_avatar_for_consistency_check")
    cutoff_timestamp = \
        datetime.datetime.now() - \
        datetime.timedelta(days=_audit_cutoff_days)
    database = AuditResultDatabase()
    ineligible_avatar_ids = set(
        database.ineligible_avatar_ids(cutoff_timestamp)
    )
    eligible_avatar_ids = state["avatar-ids"] - ineligible_avatar_ids
    log.info("found %s avatars eligible for consistency check" % (
        len(eligible_avatar_ids),
    ))
    if len(eligible_avatar_ids) == 0:
        return []

    avatar_id = random.choice(list(eligible_avatar_ids))
    return _start_consistency_check(state, avatar_id)

def _handle_anti_entropy_audit_request(state, message_body):
    """handle a requst to audit a specific avatar, not some random one"""
    log = logging.getLogger("_handle_anti_entropy_audit_request")
    message = AntiEntropyAuditRequest.unmarshall(message_body)
    log.info("request for audit on %s" % (message.avatar_id, )) 

    timestamp = datetime.datetime.now()

    database = AuditResultDatabase()
    row_id = database.start_audit(message.avatar_id, timestamp)
    database.close()

    state[message.request_id] = _request_state_tuple(
        timestamp=timestamp,
        timeout=time.time()+_request_timeout,
        timeout_function=_timeout_request,
        avatar_id=message.avatar_id,
        retry_count=_max_retry_count,
        replies=dict(), 
        row_id=row_id,
        reply_exchange=message.reply_exchange,
        reply_routing_header=message.reply_routing_header
    )

    message = DatabaseConsistencyCheck(
        message.request_id,
        message.avatar_id,
        time.mktime(timestamp.timetuple()),
        local_exchange_name,
        _routing_header
    )
    # send the DatabaseConsistencyCheck to every node
    return [
        (dest_exchange, message.routing_key, message) \
        for dest_exchange in _exchanges
    ]

def _handle_low_traffic(_state, _message_body):
    log = logging.getLogger("_handle_low_traffic")
    log.debug("ignoring low traffic message")
    return None

def _handle_database_avatar_list_reply(state, message_body):
    log = logging.getLogger("_handle_database_avatar_list_reply")
    message = DatabaseAvatarListReply.unmarshall(message_body)

    state["avatar-ids"] = set(message.get())
    log.info("found %s avatar ids" % (len(state["avatar-ids"]), ))

    # if we don't have a consistency check in progress, start one
    if not any(filter(_is_request_state, state.items())):
        return _choose_avatar_for_consistency_check(state)

    return []

def _handle_database_consistency_check_reply(state, message_body):
    log = logging.getLogger("_handle_database_consistency_check_reply")
    message = DatabaseConsistencyCheckReply.unmarshall(message_body)

    if not message.request_id in state:
        log.warn("Unknown request_id %s from %s" % (
            message.request_id, message.node_name
        ))
        return []

    request_id = message.request_id
    if message.error:
        log.error("%s (%s) %s from %s %s" % (
            state[request_id].avatar_id, 
            message.result,
            message.error_message,
            message.node_name,
            message.request_id
        ))
        hash_value = _error_hash
    else:
        hash_value = message.hash

    # if this audit was started by an AntiEntropyAuditRequest message,
    # we want to send a reply
    if state[request_id].reply_routing_header is not None:
        reply_routing_key = ".".join([
            state[request_id].reply_routing_header,
            AntiEntropyAuditReply.routing_tag
        ])
        reply_exchange = state[request_id].reply_exchange
        assert reply_exchange is not None
    else:
        reply_routing_key = None
        reply_exchange = None
        
    if message.node_name in state[request_id].replies:
        error_message = "duplicate reply from %s %s %s" % (
            message.node_name,
            state[request_id].avatar_id, 
            request_id
        )
        log.error(error_message)
        if reply_exchange is not None:
            reply_message = AntiEntropyAuditReply(
                request_id,
                AntiEntropyAuditReply.other_error,
                error_message
            )
            return [(reply_exchange, reply_routing_key, reply_message, ), ]
        else:
            return []

    state[request_id].replies[message.node_name] = hash_value

    # not done yet, wait for more replies
    if len(state[request_id].replies) < len(_exchanges):
        return []

    # at this point we should have a reply from every node, so
    # we don't want to preserve state anymore
    request_state = state.pop(request_id)
    database = AuditResultDatabase()
    timestamp = datetime.datetime.now()
    
    hash_list = list(set(request_state.replies.values()))
    
    # ok - all have the same hash
    if len(hash_list) == 1 and hash_list[0] != _error_hash:
        log.info("avatar %s compares ok" % (request_state.avatar_id, ))
        database.successful_audit(request_state.row_id, timestamp)
        if reply_exchange is not None:
            reply_message = AntiEntropyAuditReply(
                request_id,
                AntiEntropyAuditReply.successful
            )
            return [(reply_exchange, reply_routing_key, reply_message, ), ]
        else:
            return []

    # we have error(s), but the non-errors compare ok
    if len(hash_list) == 2 and _error_hash in hash_list:
        error_count = 0
        for value in request_state.replies.values():
            if value == _error_hash:
                error_count += 1

        # if we come from AntiEntropyAuditRequest, don't retry
        if reply_exchange is not None:
            database.audit_error(request_state.row_id, timestamp)
            database.close()
            error_message = "There were %s error hashes" % (error_count, )
            log.error(error_message)
            reply_message = AntiEntropyAuditReply(
                request_id,
                AntiEntropyAuditReply.other_error,
                error_message
            )
            return [(reply_exchange, reply_routing_key, reply_message, ), ]
        
        if request_state.retry_count >= _max_retry_count:
            log.error("avatar %s %s errors, too many retries" % (
                request_state.avatar_id, 
                error_count
            ))
            database.audit_error(request_state.row_id, timestamp)
            # TODO: needto do something here
        else:
            log.warn("avatar %s %s errors, will retry" % (
                request_state.avatar_id, 
                error_count
            ))
            state["retry-list"].append(
                _retry_entry_tuple(
                    retry_time=_retry_time(), 
                    avatar_id=request_state.avatar_id,
                    row_id=request_state.row_id,
                    retry_count=request_state.retry_count, 
                )
            )
            database.wait_for_retry(request_state.row_id)
        database.close()
        return []

    # if we make it here, we have some form of mismatch, possibly mixed with
    # errors
    error_message = "avatar %s hash mismatch" % (request_state.avatar_id, )
    log.error(error_message)
    for node_name, value in request_state.replies.items():
        log.error("    node %s value %s" % (node_name, value, ))

    # if we come from AntiEntropyAuditRequest, don't retry
    if reply_exchange is not None:
        database.audit_error(request_state.row_id, timestamp)
        database.close()
        reply_message = AntiEntropyAuditReply(
            request_id,
            AntiEntropyAuditReply.audit_error,
            error_message
        )
        database.audit_error(request_state.row_id, timestamp)
        database.close()
        return [(reply_exchange, reply_routing_key, reply_message, ), ]

    if request_state.retry_count >= _max_retry_count:
        log.error("%s too many retries" % (request_state.avatar_id, ))
        database.audit_error(request_state.row_id, timestamp)
        # TODO: need to do something here
    else:
        state["retry-list"].append(
            _retry_entry_tuple(
                retry_time=_retry_time(), 
                avatar_id=request_state.avatar_id,
                row_id=request_state.row_id,
                retry_count=request_state.retry_count, 
            )
        )
        database.wait_for_retry(request_state.row_id)

    database.close()
    return []

_dispatch_table = {
    AntiEntropyAuditRequest.routing_key : \
        _handle_anti_entropy_audit_request,    
    _database_avatar_list_reply_routing_key   : \
        _handle_database_avatar_list_reply,
    _database_consistency_check_reply_routing_key   : \
        _handle_database_consistency_check_reply,
    _low_traffic_routing_key            : _handle_low_traffic,
}

def _startup(halt_event, state):
    state["low_traffic_thread"] = LowTrafficThread(
        halt_event, 
        _routing_header
    )
    state["low_traffic_thread"].start()
    state["next_poll_interval"] = _next_poll_interval()
    state["next_avatar_poll_interval"] = _next_avatar_poll_interval()
    return _request_avatar_ids()

def _check_time(state):
    """check if enough time has elapsed"""
    log = logging.getLogger("_check_time")

    state["low_traffic_thread"].reset()

    current_time = time.time()

    # see if we have any retries ready
    next_retry_list = list()
    for retry_entry in state["retry-list"]:
        if current_time >= retry_entry.retry_timestamp:
            _start_consistency_check(
                state,
                retry_entry.avatar_id, 
                row_id=retry_entry.row_id,
                retry_count=retry_entry.retry_count +1)
        else:
            next_retry_list.append(retry_entry)
    state["retry-list"] = next_retry_list

    # see if we have any timeouts
    for request_id, request_state in filter(_is_request_state, state.items()):
        if current_time > request_state.timeout:
            log.warn(
                "%s timed out waiting message; running timeout function" % (
                    request_id
                )
            )
            request_state.timeout_function(request_id, state)

    # periodically send DatabaseAvatarListRequest
    if current_time >= state["next_avatar_poll_interval"]:
        state["next_avatar_poll_interval"] = _next_avatar_poll_interval()
        return _request_avatar_ids()

    if current_time >= state["next_poll_interval"]:
        state["next_poll_interval"] = _next_poll_interval()
        return _choose_avatar_for_consistency_check(state)

    return []

def _shutdown(state):
    state["low_traffic_thread"].join()
    del state["low_traffic_thread"]
    return []

if __name__ == "__main__":
    state = _create_state()
    sys.exit(
        process.main(
            _log_path, 
            _queue_name, 
            _routing_key_binding, 
            _dispatch_table, 
            state,
            pre_loop_function=_startup,
            in_loop_function=_check_time,
            post_loop_function=_shutdown
        )
    )

