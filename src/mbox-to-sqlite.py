#!/usr/bin/env python3

import argparse
import logging
import sys
from typing import List

from mbox_stats.database import build_database
from mbox_stats.mbox_file import compression_of


def main(argv: List[str]):
    parser = argparse.ArgumentParser(
        description='Build a SQLite metadata index of an mbox file. Message bodies are not stored; '
                    'they are recovered from the mbox using the stored byte offsets.')
    parser.add_argument('mbox_file', help='Path to the mbox file (optionally compressed with .gz, .bz2, .xz, or .zstd)')
    parser.add_argument('--db', help='Output SQLite database path (default: <mbox_file>.sqlite)')
    parser.add_argument('--preview-length', type=int, default=500,
                        help='Number of body characters to store for search/preview (0 to disable)')
    parser.add_argument('--batch-size', type=int, default=1000, help='Messages per insert transaction')
    parser.add_argument('--progress-interval', type=int, default=10000, help='Log progress every N messages')
    parser.add_argument('-v', '--verbose', action='store_true', help='Enable debug logging')
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')

    db_path = args.db or args.mbox_file + '.sqlite'

    if compression_of(args.mbox_file):
        logging.warning('Compressed mbox: recovering message bodies will require decompressing up to each '
                        'message and will be slow. Consider decompressing the mbox before indexing.')

    try:
        count = build_database(args.mbox_file, db_path, args.preview_length,
                               args.batch_size, args.progress_interval)
    except KeyboardInterrupt:
        logging.error('Interrupted, database not written')
        sys.exit(1)
    logging.info(f'Indexed {count} messages into {db_path}')


if __name__ == '__main__':
    main(sys.argv[1:])
