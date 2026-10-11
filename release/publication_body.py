"""Bound the complete Release body without discarding validation evidence."""
# GitHub Release API's body validation limit; fail locally before submission.
# https://github.com/cli/cli/issues/7705
MAX_CHARACTERS = 125000


def checked(text):
    if len(text) > MAX_CHARACTERS:
        raise ValueError(f'Release body exceeds {MAX_CHARACTERS} characters; evidence must not be truncated')
    return text


if __name__ == '__main__':
    import sys
    from pathlib import Path
    checked(Path(sys.argv[1]).read_text())
