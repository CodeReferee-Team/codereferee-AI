"""Transport and source-context overlay for a request-scoped validation.yaml."""
from pathlib import Path

MAX_BYTES = 64 * 1024
CONFIG_PATH = '.codereferee/validation.yaml'


def configuration_text(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value.encode('utf-8')) > MAX_BYTES:
        raise ValueError('validation_yaml must be UTF-8 text of at most 64 KiB')
    text = value.removeprefix('\ufeff').replace('\r\n', '\n').replace('\r', '\n')
    if not text.strip() or '\x00' in text:
        raise ValueError('validation_yaml must be nonempty text without NUL bytes')
    return text


def overlay_configuration(repository: Path, value: str) -> None:
    text = configuration_text(value)
    directory = repository / '.codereferee'
    path = directory / 'validation.yaml'
    if directory.is_symlink() or path.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValueError('Uploaded configuration cannot follow symlinks or replace a non-directory')
    if not path.resolve().is_relative_to(repository.resolve()):
        raise ValueError('Uploaded configuration must stay inside the repository clone')
    path.parent.mkdir(exist_ok=True)
    path.write_text(text, encoding='utf-8', newline='\n')
