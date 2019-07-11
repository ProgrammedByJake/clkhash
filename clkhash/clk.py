"""
Generate CLK from data.
"""

import concurrent.futures
import logging
import time
from typing import (AnyStr, Callable, cast, Iterable, List, Optional,
                    Sequence, TextIO, Tuple, TypeVar, Union)
from future.builtins import range

from clkhash.backports import unicode_reader
from clkhash.bloomfilter import stream_bloom_filters
from clkhash.serialization import serialize_bitarray
from clkhash.key_derivation import generate_key_lists
from clkhash.schema import Schema
from clkhash.validate_data import (validate_entries, validate_header,
                                   validate_row_lengths)


log = logging.getLogger('clkhash.clk')

CHUNK_SIZE = 1000


def hash_and_serialize_chunk(chunk_pii_data,  # type: Sequence[Sequence[str]]
                             keys,  # type: Sequence[Sequence[bytes]]
                             schema  # type: Schema
                             ):
    # type: (...) -> Tuple[List[str], Sequence[int]]
    """
    Generate Bloom filters (ie hash) from chunks of PII then serialize
    the generated Bloom filters. It also computes and outputs the Hamming weight (or popcount) -- the number of bits
    set to one -- of the generated Bloom filters.

    :param chunk_pii_data: An iterable of indexable records.
    :param keys: A tuple of two lists of secret keys used in the HMAC.
    :param Schema schema: Schema specifying the entry formats and
            hashing settings.
    :return: A list of serialized Bloom filters and a list of corresponding popcounts
    """
    clk_data = []
    clk_popcounts = []
    for clk in stream_bloom_filters(chunk_pii_data, keys, schema):
        clk_data.append(serialize_bitarray(clk[0]).strip())
        clk_popcounts.append(clk[2])
    return clk_data, clk_popcounts


def generate_clk_from_csv(input_f,  # type: TextIO
                          keys,  # type: Tuple[AnyStr, AnyStr]
                          schema,  # type: Schema
                          validate=True,  # type: bool
                          header=True,  # type: Union[bool, AnyStr]
                          progress_bar=None  # type: Optional[object]
                          ):
    # type: (...) -> List[str]
    """ Generate Bloom filters from CSV file, then serialise them.

        This function also computes and outputs the Hamming weight
        (a.k.a popcount -- the number of bits set to high) of the
        generated Bloom filters.

        :param input_f: A file-like object of csv data to hash.
        :param keys: A tuple of two lists of secret keys.
        :param schema: Schema specifying the record formats and
            hashing settings.
        :param validate: Set to `False` to disable validation of
            data against the schema. Note that this will silence
            warnings whose aim is to keep the hashes consistent between
            data sources; this may affect linkage accuracy.
        :param header: Set to `False` if the CSV file does not have
            a header. Set to `'ignore'` if the CSV file does have a
            header but it should not be checked against the schema.
        :param progress_callback: A callable to update the progress to
            the user.
        :return: A list of serialized Bloom filters and a list of
            corresponding popcounts.
    """
    if header not in {False, True, 'ignore'}:
        raise ValueError("header must be False, True or 'ignore' but is {}."
                         .format(header))

    log.info("Hashing data")

    # Read from CSV file
    reader = unicode_reader(input_f)

    if header:
        column_names = next(reader)
        if header != 'ignore':
            validate_header(schema.fields, column_names)

    start_time = time.time()

    # Read the lines in CSV file and add it to PII
    pii_data = []
    for line in reader:
        pii_data.append(tuple(element.strip() for element in line))

    validate_row_lengths(schema.fields, pii_data)

    progress_callback = None
    # As we are separating tqdm from the clk module, we don't want to import ProgressBar
    # We explicitly test for attributes and ignore mypys checks.
    if hasattr(progress_bar, 'initialise') and hasattr(progress_bar, 'callback'):
        progress_bar.initialise(len(pii_data))  # type: ignore
        progress_callback = progress_bar.callback  # type: ignore

    results = generate_clks(pii_data,
                            schema,
                            keys,
                            validate=validate,
                            callback=progress_callback)

    if hasattr(progress_bar, 'close'):
        progress_bar.close()  # type: ignore

    log.info("Hashing took {:.2f} seconds".format(time.time() - start_time))
    return results


def generate_clks(pii_data,  # type: Sequence[Sequence[str]]
                  schema,  # type: Schema
                  keys,  # type: Tuple[AnyStr, AnyStr]
                  validate=True,  # type: bool
                  callback=None  # type: Optional[Callable[[int, Sequence[int]], None]]
                  ):
    # type: (...) -> List[str]

    # generate two keys for each identifier
    key_lists = generate_key_lists(
        keys,
        len(schema.fields),
        key_size=schema.kdf_key_size,
        salt=schema.kdf_salt,
        info=schema.kdf_info,
        kdf=schema.kdf_type,
        hash_algo=schema.kdf_hash)

    if validate:
        validate_entries(schema.fields, pii_data)

    # Chunks PII
    log.info("Hashing {} entities".format(len(pii_data)))
    chunk_size = 200 if len(pii_data) <= 10000 else 1000
    futures = []

    # Compute Bloom filter from the chunks and then serialise it
    with concurrent.futures.ProcessPoolExecutor() as executor:
        for chunk in chunks(pii_data, chunk_size):
            future = executor.submit(
                hash_and_serialize_chunk,
                chunk, key_lists, schema, )
            if callback is not None:
                unpacked_callback = cast(Callable[[int, Sequence[int]], None],
                                         callback)
                future.add_done_callback(
                    lambda f: unpacked_callback(len(f.result()[0]),
                                                f.result()[1]))
            futures.append(future)

        results = []
        for future in futures:
            clks, clk_stats = future.result()
            results.extend(clks)

    return results


T = TypeVar('T')  # Declare generic type variable


def chunks(seq, chunk_size):
    # type: (Sequence[T], int) -> Iterable[Sequence[T]]
    """ Split seq into chunk_size-sized chunks.

        :param seq: A sequence to chunk.
        :param chunk_size: The size of chunk.
    """
    return (seq[i:i + chunk_size] for i in range(0, len(seq), chunk_size))
