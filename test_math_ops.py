"""Unit tests for src.math_ops."""

from src.math_ops import add, subtract


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(5, 3) == 2
