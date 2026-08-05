# braindamage

A CS2 trade-up contract simulator: browse the skin catalog, fetch prices, design and simulate trade-up contracts (with EV/ROI/CVaR), and batch-survey mono trades. Desktop UI built with PySide6 (Qt for Python).

## Requirements

- Python >= 3.14
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
uv sync
```

## Running

```bash
uv run main.py
```

(equivalently: activate the project's virtualenv and run `python main.py`)

## Tests

```bash
uv run pytest
```
