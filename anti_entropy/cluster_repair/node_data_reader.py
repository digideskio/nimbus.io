# -*- coding: utf-8 -*-
"""
node_data_reader.py

manage 10 subprocesses to read data from nodes
"""
import heapq
import itertools
import logging
import os 
import subprocess
import sys
from threading import Event

from tools.standard_logging import initialize_logging
from tools.sized_pickle import store_sized_pickle, retrieve_sized_pickle
from tools.process_util import identify_program_dir, set_signal_handler

class NodeDataReaderError(Exception):
    pass

_local_node_name = os.environ["NIMBUSIO_NODE_NAME"]
_log_path = "{0}/nimbusio_cluster_repair_data_reader_{1}.log".format(
    os.environ["NIMBUSIO_LOG_DIR"], _local_node_name)
_node_names = os.environ["NIMBUSIO_NODE_NAME_SEQ"].split()
_read_buffer_size = int(
    os.environ.get("NIMBUSIO_ANTI_ENTROPY_READ_BUFFER_SIZE", 
                   str(10 * 1024 ** 2)))


def _node_generator(halt_event, node_name, node_subprocess):
    log = logging.getLogger(node_name)
    while not halt_event.is_set():
        try:
            yield retrieve_sized_pickle(node_subprocess.stdout)
        except EOFError:
            log.info("EOFError, assuming processing complete")
            break

    returncode = node_subprocess.poll()
    if returncode is None:
        log.warn("subprocess still running")
        node_subprocess.terminate()
    log.debug("waiting for subprocess to terminate")
    returncode = node_subprocess.wait()
    if returncode == 0:
        log.debug("subprocess terminated normally")
    else:
        log.warn("subprocess returned {0}".format(returncode))

def _start_subprocesses(halt_event):
    """
    start subprocesses
    """
    log = logging.getLogger("start_subprocesses")
    node_generators = list()

    anti_entropy_dir = identify_program_dir("anti_entropy")
    subprocess_path = os.path.join(anti_entropy_dir,
                               "cluster_repair",
                               "node_data_reader_subprocess.py")

    for index, node_name in enumerate(_node_names):

        if halt_event.is_set():
            log.info("halt_event set: exiting")
            return node_generators

        log.info("starting subprocess {0}".format(node_name))
        args = [sys.executable, subprocess_path, str(index) ]
        process = subprocess.Popen(args, 
                                   bufsize=_read_buffer_size,
                                   stdout=subprocess.PIPE)
        assert process is not None
        node_generators.append(_node_generator(halt_event, node_name, process))

    return node_generators

def _group_key_function(node_data):
    sequence_key = node_data[0]
    unified_id, conjoined_part, sequence_num, _segment_num = sequence_key
    return (unified_id, conjoined_part, sequence_num, )

def _manage_subprocesses(halt_event, merge_manager):
    log = logging.getLogger("_manage_subprocesses")
    group_object = itertools.groupby(merge_manager, _group_key_function)
    for (unified_id, conjoined_part, sequence_num), node_group in group_object:
        group_dict = {
            "unified_id"        : unified_id,
            "conjoined_part"    : conjoined_part,
            "sequence_num"      : sequence_num,
            "segment_status"    : None,
            "node_data"         : list()
        }
        if  halt_event.is_set():
            log.warn("halt_event set, exiting")
            break
        for (_sequence_key, segment_status, node_data, ) in node_group:
            if group_dict["segment_status"] is None:
                group_dict["segment_status"] = segment_status
            assert segment_status == group_dict["segment_status"]
            group_dict["node_data"].append(node_data)
    
        log.debug("group: unified_id={0}, conjoined_part={1}, "
                  "sequence_num={2}, segment_status={3}".format(
            group_dict["unified_id"], 
            group_dict["conjoined_part"],
            group_dict["sequence_num"],
            group_dict["segment_status"]))

        store_sized_pickle(group_dict, sys.stdout.buffer)

def main():
    """
    main entry point

    return 0 for success (exit code)
    """
    return_value = 0

    initialize_logging(_log_path)
    log = logging.getLogger("main")
    log.info("program starts")

    halt_event = Event()
    set_signal_handler(halt_event)

    node_generators = _start_subprocesses(halt_event)
    merge_manager = heapq.merge(*node_generators)

    try:
        _manage_subprocesses(halt_event, merge_manager)
    except Exception as instance:
        log.exception(instance)
        return_value = 1

    return return_value

if __name__ == "__main__":
    sys.exit(main())


