from email.utils import parseaddr


def supplier_from_sender(sender: str) -> str | None:
    """Use the display name when present, otherwise the sender's domain."""
    name, address = parseaddr(sender or "")
    name = name.strip().strip('"')
    if name:
        return name
    if "@" in address:
        return address.split("@", 1)[1]
    return sender.strip() or None
