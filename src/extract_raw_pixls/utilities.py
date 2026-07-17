#  SPDX-FileCopyrightText: 2026 Damon Lynch <damonlynch@gmail.com>
#  SPDX-License-Identifier: GPL-3.0-or-later

import math


def format_bytes(bytes_value: int) -> str:
    """
    Convert bytes to a human-readable format.

    :param bytes_value: The bytes value to convert to a human-readable format.

    >>> format_bytes(1049866240)
    '1001 MB'
    >>> format_bytes(1024)
    '1 KB'
    """
    if bytes_value == 0:
        return "0 B"

    # Standard labels for binary storage units
    units = ("B", "KB", "MB", "GB", "TB", "PB", "EB")

    # Calculate the base 1024 exponent index
    # e.g., 1024 -> index 1 (KB), 1048576 -> index 2 (MB)
    i = int(math.floor(math.log(bytes_value, 1024)))

    # Scale the bytes to the correct unit value
    p = math.pow(1024, i)
    scaled_value = bytes_value / p

    return f"{scaled_value:.0f} {units[i]}"
