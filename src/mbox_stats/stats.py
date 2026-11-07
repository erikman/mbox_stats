import mailbox
import datetime
import pathlib
import logging
from email.header import decode_header

from mbox_stats.mbox_reader import MboxReader

logger = logging.getLogger(__name__)


def message_identification(message: mailbox.Message) -> str:
    subject = message['Subject'] or ''
    sender = message['From'] or ''
    date = message['Date'] or ''
    return f"Message [{subject} | {sender} | {date}]"


def human_readable_size(size_bytes):
    if size_bytes == 0:
        return "0B"
    size_name = ("B", "KB", "MB", "GB", "TB")
    i = 0
    while size_bytes >= 1024 and i < len(size_name) - 1:
        size_bytes /= 1024.0
        i += 1
    return f"{size_bytes:.2f} {size_name[i]}"


def decode_mime_header_value(value: str) -> str:
    parts = []
    for decoded_part, charset in decode_header(value):
        if isinstance(decoded_part, bytes):
            encoding = charset or 'utf-8'
            parts.append(decoded_part.decode(encoding, errors='replace'))
        else:
            parts.append(decoded_part)
    return ''.join(parts)


class BasicStats:
    _total_messages = 0
    _total_size = 0

    def process(self, message: mailbox.Message, message_size: int):
        self._total_messages += 1
        self._total_size += message_size

    def finalize(self, output):
        output.write(f"Total messages: {self._total_messages}\n")
        output.write(f"Total size: {human_readable_size(self._total_size)}\n")


class SizePerMonth:
    _size_per_month = {}
    _count_per_month = {}
    _size_per_year = {}
    _count_per_year = {}

    def process(self, message: mailbox.Message, message_size: int):
        date = message['Date']
        if date:
            try:
                date = datetime.datetime.strptime(date, '%a, %d %b %Y %H:%M:%S %z')
            except ValueError:
                logger.warning(f"SizePerMonth: Failed to parse date: \"{date}\", {message_identification(message)} skipping message")
                return

            month = date.strftime('%Y-%m')
            year = date.strftime('%Y')
            self._size_per_month[month] = self._size_per_month.get(month, 0) + message_size
            self._count_per_month[month] = self._count_per_month.get(month, 0) + 1
            self._size_per_year[year] = self._size_per_year.get(year, 0) + message_size
            self._count_per_year[year] = self._count_per_year.get(year, 0) + 1

    def finalize(self, output):
        output.write("Size per month:\n")
        for month, size in sorted(self._size_per_month.items(), key=lambda x: x[0], reverse=True):
            count = self._count_per_month.get(month, 0)
            output.write(f"{month}: {human_readable_size(size)} ({count} messages)\n")

        output.write("\nSize per year:\n")
        for year, size in sorted(self._size_per_year.items(), key=lambda x: x[0], reverse=True):
            count = self._count_per_year.get(year, 0)
            output.write(f"{year}: {human_readable_size(size)} ({count} messages)\n")


class SizePerGMailLabel:
    _size_per_label = {}
    _count_per_label = {}

    _size_per_combined_label = {}
    _count_per_combined_label = {}

    def _add_to_label(self, label: str, size: int):
        self._size_per_label[label] = self._size_per_label.get(label, 0) + size
        self._count_per_label[label] = self._count_per_label.get(label, 0) + 1

    def process(self, message: mailbox.Message, message_size: int):
        labels = message['X-Gmail-Labels']
        if labels:
            labels = decode_mime_header_value(labels)

            self._size_per_combined_label[labels] = self._size_per_combined_label.get(labels, 0) + message_size
            self._count_per_combined_label[labels] = self._count_per_combined_label.get(labels, 0) + 1

            for label in labels.split(','):
                label = label.strip()
                self._add_to_label(label, message_size)
        else:
            self._add_to_label('(no label)', message_size)

    def finalize(self, output):
        output.write("Size per Gmail label:\n")
        for label, size in sorted(self._size_per_label.items(), key=lambda x: x[1], reverse=True):
            count = self._count_per_label.get(label, 0)
            output.write(f"{label}: {human_readable_size(size)} ({count} messages)\n")

        output.write("\nSize per combined Gmail label:\n")
        for label, size in sorted(self._size_per_combined_label.items(), key=lambda x: x[1], reverse=True):
            count = self._count_per_combined_label.get(label, 0)
            output.write(f"{label}: {human_readable_size(size)} ({count} messages)\n")


class NumberOfMessagesPerSize:
    _size_bins = {
        '0-1KB': 0,
        '1KB-10KB': 0,
        '10KB-100KB': 0,
        '100KB-1MB': 0,
        '1MB-10MB': 0,
        '10MB+': 0
    }
    _count_bins = {
        '0-1KB': 0,
        '1KB-10KB': 0,
        '10KB-100KB': 0,
        '100KB-1MB': 0,
        '1MB-10MB': 0,
        '10MB+': 0
    }

    def process(self, message: mailbox.Message, message_size: int):
        size = message_size
        if size < 1024:
            self._size_bins['0-1KB'] += size
            self._count_bins['0-1KB'] += 1
        elif size < 10240:
            self._size_bins['1KB-10KB'] += size
            self._count_bins['1KB-10KB'] += 1
        elif size < 102400:
            self._size_bins['10KB-100KB'] += size
            self._count_bins['10KB-100KB'] += 1
        elif size < 1048576:
            self._size_bins['100KB-1MB'] += size
            self._count_bins['100KB-1MB'] += 1
        elif size < 10485760:
            self._size_bins['1MB-10MB'] += size
            self._count_bins['1MB-10MB'] += 1
        else:
            self._size_bins['10MB+'] += size
            self._count_bins['10MB+'] += 1

    def finalize(self, output):
        output.write("Number of messages per size bin:\n")
        for bin, size in self._size_bins.items():
            count = self._count_bins.get(bin, 0)
            output.write(f"{bin}: {human_readable_size(size)} ({count} messages)\n")


class MessageSizePerSenderDomain:
    _size_per_sender = {}
    _count_per_sender = {}

    _size_per_domain = {}
    _count_per_domain = {}

    def process(self, message: mailbox.Message, message_size: int):
        sender = message['From']
        if sender:
            self._size_per_sender[sender] = self._size_per_sender.get(sender, 0) + message_size
            self._count_per_sender[sender] = self._count_per_sender.get(sender, 0) + 1

            domain = sender.split('@')[-1].rstrip('>')
            self._size_per_domain[domain] = self._size_per_domain.get(domain, 0) + message_size
            self._count_per_domain[domain] = self._count_per_domain.get(domain, 0) + 1

    def finalize(self, output):
        output.write("Message size per sender (+10MB):\n")
        for sender, size in sorted(self._size_per_sender.items(), key=lambda x: x[1], reverse=True):
            if size > 10*1024*1024:  # Only show senders with more than 10MB of data
                count = self._count_per_sender.get(sender, 0)
                output.write(f"{sender}: {human_readable_size(size)} ({count} messages)\n")

        output.write("\nMessage size per sender domain (+1MB):\n")
        for domain, size in sorted(self._size_per_domain.items(), key=lambda x: x[1], reverse=True):
            if size > 1024*1024:  # Only show domains with more than 1MB of data
                count = self._count_per_domain.get(domain, 0)
                output.write(f"{domain}: {human_readable_size(size)} ({count} messages)\n")


def calculate_stats(mailbox: MboxReader, output):
    stats = [
        BasicStats(),
        SizePerMonth(),
        NumberOfMessagesPerSize(),
        MessageSizePerSenderDomain(),
        SizePerGMailLabel(),
    ]

    start_time = datetime.datetime.now()
    try:
        for message, message_size in mailbox:
            # TODO: Add progress meter
            # TODO: Print elapsed time and estimated time remaining
            for stat in stats:
                try:
                    stat.process(message, message_size)
                except Exception as e:
                    logger.error(f"Error processing message {message_identification(message)}: {e}", exc_info=True)
    except KeyboardInterrupt:
        logger.info("Processing interrupted by user. Finalizing stats...")

    logger.info(f"Processing completed in {datetime.datetime.now() - start_time}")

    for stat in stats:
        try:
            stat.finalize(output)
        except Exception as e:
            logger.error(f"Error finalizing stats {stat.__class__.__name__}: {e}", exc_info=True)
        output.write("\n")


