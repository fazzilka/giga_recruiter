def vacancy_identity(url):
    """Sber numeric ID is stable across trailing slashes and title redirects."""
    import re
    from urllib.parse import urlsplit

    parsed = urlsplit(url or "")
    match = re.search(r"(?:/|-)(\d+)/?$", parsed.path)
    return (
        (parsed.hostname, match.group(1))
        if match
        else (parsed.hostname, parsed.path.rstrip("/"))
    )
