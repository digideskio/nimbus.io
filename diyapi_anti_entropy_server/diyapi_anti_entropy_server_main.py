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
import logging
import os
import sys
import time

from diyapi_tools import message_driven_process as process
from diyapi_tools.low_traffic_thread import LowTrafficThread, \
        low_traffic_routing_tag

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
_database_consistency_check_reply_routing_key = ".".join([
    _routing_header,
    DatabaseConsistencyCheckReply.routing_tag,
])
_low_traffic_routing_key = ".".join([
    _routing_header, 
    low_traffic_routing_tag,
])
_polling_interval = float(os.environ.get(
    "DIYAPI_ANTI_ENTROPY_POLLING_INTERVAL", "600.0")
)

def _create_state():
    return dict()

def _next_poll_interval():
    return time.time() + _polling_interval

def _handle_low_traffic(_state, _message_body):
    log = logging.getLogger("_handle_low_traffic")
    log.debug("ignoring low traffic message")
    return None

def _handle_database_consistency_check_reply(state, message_body):
    log = logging.getLogger("_handle_database_consistency_check_reply")
    message = DatabaseConsistencyCheckReply.unmarshall(message_body)

    return []

_dispatch_table = {
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

    return []

def _check_time(state):
    """check if enough time has elapsed"""
    log = logging.getLogger("_check_time")

    state["low_traffic_thread"].reset()

    if time.time() < state["next_poll_interval"]:
        return []

    state["next_poll_interval"] = _next_poll_interval()

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

