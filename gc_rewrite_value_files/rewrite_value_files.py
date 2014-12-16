# -*- coding: utf-8 -*-
"""
rewrite_value_files.py
"""
from collections import defaultdict
import hashlib
import logging
import operator
import os

from tools.data_definitions import compute_value_file_path
from tools.file_space import load_file_space_info, \
                             file_space_sanity_check, \
                             find_least_volume_space_id_and_path, \
                             available_space_on_volume

from tools.output_value_file import OutputValueFile

class GCInsufficientSpaceError(Exception):
    """
    Ticket #5866 Nimbus.io should avoid filling disks to capacity
    """
    pass

_max_value_file_size = int(os.environ.get(
    "NIMBUS_IO_MAX_VALUE_FILE_SIZE", str(1024 ** 3))
)
_min_destination_space = long(
    os.environ.get("NIMBUS_IO_GC_MIN_DESTINATION_SPACE",
        str(5 * 1024 ** 3)))

def _allocate_output_value_files(connection, repository_path, refs):
    log = logging.getLogger("_allocate_output_value_files")
    output_value_file_sizes = defaultdict(list)

    file_space_info = load_file_space_info(connection) 
    file_space_sanity_check(file_space_info, repository_path)

    for ref in refs:
        if len(output_value_file_sizes[ref.collection_id]) == 0:
            output_value_file_sizes[ref.collection_id].append(0)

        expected_size = output_value_file_sizes[ref.collection_id][-1] \
                      + ref.data_size

        if expected_size > _max_value_file_size:
            output_value_file_sizes[ref.collection_id].append(0)

        output_value_file_sizes[ref.collection_id][-1] += ref.data_size

    output_value_files = defaultdict(list)
    for collection_id in output_value_file_sizes.keys():
        for expected_size in output_value_file_sizes[collection_id]:
            space_id, space_path = \
                find_least_volume_space_id_and_path("storage", file_space_info)

            # Ticket #5866 Nimbus.io should avoid filling disks to capacity
            available_space = available_space_on_volume(space_path)
            if available_space - expected_size < _min_destination_space:
                log.warn("Insufficient space: available {0}, value_file {1}, "
                         "minimum {2}".format(
                    available_space, expected_size, _min_destination_space))
                for output_value_file_list in output_value_files.values():
                    for output_value_file in output_value_file_list:
                        output_value_file.purge()
                raise GCInsufficientSpaceError("insufficient space")

            output_value_files[collection_id].append(
                OutputValueFile(connection, 
                                space_id,
                                repository_path, 
                                expected_size=expected_size))

    return output_value_files

def _process_batch(connection, repository_path, refs, value_file_data):
    log = logging.getLogger("_process_batch")

    # Sort the records by segment.collection_id, segment.key and 
    # segment.unified_id.
    refs.sort(key=operator.attrgetter("collection_id", "key", "unified_id"))

    # Determine the number and sizes of target files needed: 
    # At least one value file per collection ID, 
    # no file larger than the standard max size of a value file.
    output_value_files = _allocate_output_value_files(connection, 
                                                      repository_path, refs)

    # Within each target file, records sorted by key and unified_id.
    work_collection_id = None
    value_files = None
    index = 0
    for ref in refs:
        if work_collection_id is None:
            work_collection_id = ref.collection_id
            value_files = output_value_files[work_collection_id]
            index = 0
        elif ref.collection_id != work_collection_id:
            assert value_files[-1].size == value_files[-1].expected_size, \
                (value_files[-1].size, value_files[-1].expected_size)
                
            work_collection_id = ref.collection_id
            value_files = output_value_files[work_collection_id]
            index = 0
        elif value_files[index].size == value_files[index].expected_size:
            index += 1

        value_file_key = (ref.value_file_id, ref.space_id, )
        data_block = value_file_data[value_file_key]
        data = data_block[
            ref.value_file_offset:ref.value_file_offset+ref.data_size
        ]
        data_md5 = hashlib.md5(data)
        if data_md5.digest() != bytes(ref.data_hash):
            log.error(
                "md5 mismatch {0} {1} {2} {3} {4} {5} {6} {7}".format(
                    ref.segment_id,
                    ref.collection_id, 
                    ref.key, 
                    ref.unified_id,
                    ref.sequence_num,
                    ref.value_file_id,
                    ref.value_file_offset,
                    ref.data_size
                )
            )
            #TODO - insert into repair table
            continue

        value_file_offset = value_files[index].size
        value_files[index].write_data_for_one_sequence(
            ref.collection_id, ref.segment_id, data
        )

        # adjust segment_sequence row
        connection.execute("""
            update nimbusio_node.segment_sequence
            set value_file_id = %s, value_file_offset = %s
            where collection_id = %s and segment_id = %s
            and sequence_num = %s
        """, [value_files[index].value_file_id, 
              value_file_offset,
              ref.collection_id,
              ref.segment_id,
              ref.sequence_num])

    # heave all the old value files from the database
    for value_file_id, _space_id in value_file_data.keys():
        connection.execute("""
            delete from nimbusio_node.value_file 
            where id = %s""", [value_file_id, ])

    output_size = 0
    # close al the output value files, forcing database update
    for value_files in output_value_files.values():
        for value_file in value_files:
            output_size += value_file.size
            value_file.close()

    return output_size

def _remove_old_value_files(repository_path, value_file_keys):
    log = logging.getLogger("_remove_old_value_files")
    for value_file_id, space_id in value_file_keys:
        value_file_path = \
                compute_value_file_path(repository_path, 
                                        space_id, 
                                        value_file_id)
        try:
            os.unlink(value_file_path)
        except Exception:       
            log.exception(value_file_path)

def rewrite_value_files(options, connection, repository_path, ref_generator):
    log = logging.getLogger("_rewrite_value_files")
    max_sort_mem = options.max_sort_mem * 1024 ** 3

    total_batch_size = 0
    total_output_size = 0
    savings = 0

    batch_size = 0
    refs = list()
    value_file_data = dict()

    while True:

        try:
            ref = next(ref_generator)
        except StopIteration:
            break

        # this should be the start of a partition
        assert ref.value_row_num == 1, ref

        if batch_size + ref.value_file_size > max_sort_mem:
            connection.begin_transaction()
            try:
                output_size = _process_batch(connection, 
                                             repository_path, 
                                             refs, 
                                             value_file_data)
            except Exception:
                connection.rollback()
                raise
            connection.commit()
            _remove_old_value_files(repository_path, value_file_data.keys())

            total_batch_size += batch_size
            total_output_size += output_size
            savings = batch_size - output_size
            log.debug(
                "batch_size={0:,}, output_size={1:,}, savings={2:,}".format(
                    batch_size, output_size, savings
            ))

            batch_size = 0
            refs = list()
            value_file_data = dict()
            
        batch_size += ref.value_file_size

        # get the value file data
        # TODO: we should only store the actual references from the files into 
        # memory, not keep the whole files into memory.  Keeping the whole file 
        # in memory means we're using memory for parts of the files that are 
        # garbage, effectively decreasing the size of our output sort batch.  
        # We could end up with very small outputs from each batch if a large 
        # portion of the input value files are garbage.

        value_file_key = (ref.value_file_id, ref.space_id, )
        assert value_file_key not in value_file_data
        value_file_path = \
                compute_value_file_path(repository_path, 
                                        ref.space_id, 
                                        ref.value_file_id)
        with open(value_file_path, "rb") as input_file:
            value_file_data[value_file_key] = input_file.read()

        # load up the refs for this partition
        refs.append(ref)
        for _ in range(ref.value_row_count-1):
            refs.append(next(ref_generator)) 

    if len(refs) > 0:
        connection.begin_transaction()
        try:
            output_size = _process_batch(connection, 
                                         repository_path, 
                                         refs, 
                                         value_file_data)
        except Exception:
            connection.rollback()
            raise

        connection.commit()
        _remove_old_value_files(repository_path, value_file_data.keys())

        total_batch_size += batch_size
        total_output_size += output_size
        savings = batch_size - output_size
        log.debug("batch_size={0:,}, output_size={1:,}, savings={2:,}".format(
            batch_size, output_size, savings
        ))

    savings = total_batch_size - total_output_size
    log.info(
        "total_batch_size={0:,} total_output_size={1:,} savings={2:,}".format(
            total_batch_size, total_output_size, savings
    ))

    return savings

