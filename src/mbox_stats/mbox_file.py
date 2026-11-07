import bz2
import gzip
import lzma


def compression_of(mbox_file: str) -> str | None:
    for ext in ('gz', 'bz2', 'xz', 'zstd'):
        if mbox_file.endswith('.' + ext):
            return ext
    return None


def open_mbox_file(mbox_file: str):
    compression = compression_of(mbox_file)
    if compression == 'gz':
        return gzip.open(mbox_file, 'rb')
    elif compression == 'bz2':
        return bz2.open(mbox_file, 'rb')
    elif compression == 'xz':
        return lzma.open(mbox_file, 'rb')
    elif compression == 'zstd':
        try:
            import compression.zstd
        except ImportError as e:
            raise ImportError("zstd support requires Python 3.14+ (compression.zstd module).") from e
        return compression.zstd.open(mbox_file, 'rb')
    else:
        return open(mbox_file, 'rb')
