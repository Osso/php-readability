# php-readability

Readability audit CLI for PHP code.

```bash
php-readability [path] [--exclude dir1,dir2] [--fix] [--json] [--write-plan]
```

The binary embeds the audit script at compile time and runs it with
`/usr/bin/python3`, so agents can invoke `php-readability` directly instead of
running a skill-local Python file.

## Install

```bash
cargo install --path .
```

## Development

```bash
cargo test
cargo run -- /path/to/php/project --json
```
