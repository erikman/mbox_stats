#!/usr/bin/env python3

import argparse
import sys
from typing import List
import bz2
import gzip
import lzma
import logging

from mbox_stats.stats import calculate_stats
from mbox_stats.mbox_reader import MboxReader


logging.basicConfig(level=logging.INFO)

def open_mbox_file(mbox_file: str):
    if mbox_file.endswith('.gz'):
        return gzip.open(mbox_file, 'rb')
    elif mbox_file.endswith('.bz2'):
        return bz2.open(mbox_file, 'rb')
    elif mbox_file.endswith('.xz'):
        return lzma.open(mbox_file, 'rb')
    elif mbox_file.endswith('.zstd'):
        try:
            import compression.zstd
            return compression.zstd.open(mbox_file, 'rb')
        except ImportError:
            logging.error("zstd support requires the 'compression' library. Please install it with 'pip install compression' or upgrade to Python 3.14+.")
            sys.exit(1)
    else:
        return open(mbox_file, 'rb')


def open_output_file(output: str):
    if output == '-':
        return sys.stdout
    else:
        return open(output, 'w')


def main(args: List[str]):
    parser = argparse.ArgumentParser(description='Process mbox files and generate statistics.')
    parser.add_argument('mbox_file', help='Path to the mbox file to be processed (optionally compressed with .gz, .bz2, .xz, or .zstd)')
    parser.add_argument('--output', '-o', help='Path to the output file for statistics, or - for stdout', default='-')
    args = parser.parse_args(args)

    with open_mbox_file(args.mbox_file) as mbox_fh:
        mbox = MboxReader(mbox_fh)

        with open_output_file(args.output) as output_fh:
            calculate_stats(mbox, output_fh)


if __name__ == '__main__':
    main(sys.argv[1:])