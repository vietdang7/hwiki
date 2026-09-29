"""Exceptions with the CLI exit codes of INTERFACE §1."""

from __future__ import annotations


class HwsError(Exception):
    """Base error; ``exit_code`` is what the CLI returns."""

    exit_code = 1


class UsageError(HwsError):
    exit_code = 2


class SkillsInvisibleError(HwsError):
    """The agent's session did not list the active skills (D1): never scored."""

    exit_code = 3


class HermesIncompatibleError(HwsError):
    """The Hermes binary is missing or lacks a flag the framework needs (D6)."""

    exit_code = 4


class WorkspaceBusyError(HwsError):
    """Another process holds the workspace lock (D14)."""

    exit_code = 75


class RolloutError(HwsError):
    """Hermes could not launch/complete a rollout; the result is NOT a score (D7)."""


class GitError(HwsError):
    """A git operation on skills/active or wiki/ failed (D15)."""


class UnsafePathError(HwsError, ValueError):
    """A task/proposal path escapes its root (D10, D11)."""
