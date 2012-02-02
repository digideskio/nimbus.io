# -*- coding: utf-8 -*-
"""
garbage_collector_main.py
"""
import io
import logging
import os
import signal
import sys
from threading import Event

import zmq

from tools.standard_logging import initialize_logging
from tools.database_connection import get_node_local_connection
from tools.event_push_client import EventPushClient, unhandled_exception_topic

from gc_rewrite_value_files.options import get_options
from gc_rewrite_value_files.unused_value_files import \
        unlink_totally_unused_value_files

_local_node_name = os.environ["NIMBUSIO_NODE_NAME"]
_log_path = "{0}/nimbusio_gc_rewrite_value_files_{1}.log".format(
    os.environ["NIMBUSIO_LOG_DIR"], _local_node_name,
)
_repository_path = os.environ["NIMBUSIO_REPOSITORY_PATH"]

def _rewrite_value_files(options, connection, event_push_client):
    pass

def main():
    """
    main entry point
    return 0 for success (exit code)
    """
    initialize_logging(_log_path)
    log = logging.getLogger("main")
    log.info("program starts")

    options = get_options()

    try:
        connection = get_node_local_connection()
    except Exception as instance:
        log.exception("Exception connecting to database")
        return -1

    zmq_context =  zmq.Context()

    event_push_client = EventPushClient(zmq_context, "garbage_collector")
    event_push_client.info("program-start", "garbage_collector starts")  

    return_code = 0

    try:
        unlink_totally_unused_value_files(connection, _repository_path)
        _rewrite_value_files(options, connection, event_push_client)
    except Exception:
        log.exception("_garbage_collection")
        return_code = -2
    else:
        log.info("program terminates normally")

    connection.close()

    event_push_client.close()
    zmq_context.term()

    return return_code

if __name__ == "__main__":
    sys.exit(main())
