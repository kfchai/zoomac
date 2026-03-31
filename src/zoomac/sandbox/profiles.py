"""Sandbox permission profiles for Docker containers."""

from __future__ import annotations

from enum import Enum
from pydantic import BaseModel, Field


class ProfileName(str, Enum):
    MINIMAL = "minimal"
    STANDARD = "standard"
    PROJECT = "project"
    FULL = "full"


class SandboxProfile(BaseModel):
    """Defines what a sandboxed container is allowed to do."""

    name: ProfileName
    network: bool = Field(description="Whether network access is allowed")
    mounts: list[dict[str, str]] = Field(default_factory=list, description="Volume mounts [{source, target, mode}]")
    read_only_root: bool = Field(default=True, description="Root filesystem is read-only")
    cpu_limit: float = Field(default=1.0, description="CPU limit (number of cores)")
    memory_limit: str = Field(default="512m", description="Memory limit (Docker format)")
    timeout_seconds: int = Field(default=60, description="Execution timeout")
    tmpfs_size: str = Field(default="1g", description="Size of /workspace tmpfs")
    privileged: bool = Field(default=False, description="Never true — safety invariant")
    docker_socket: bool = Field(default=False, description="Never true — safety invariant")


# Pre-defined profiles
PROFILES: dict[ProfileName, SandboxProfile] = {
    ProfileName.MINIMAL: SandboxProfile(
        name=ProfileName.MINIMAL,
        network=False,
        mounts=[],
        read_only_root=True,
        cpu_limit=1.0,
        memory_limit="512m",
        timeout_seconds=60,
        tmpfs_size="1g",
    ),
    ProfileName.STANDARD: SandboxProfile(
        name=ProfileName.STANDARD,
        network=True,
        mounts=[],
        read_only_root=True,
        cpu_limit=2.0,
        memory_limit="1g",
        timeout_seconds=120,
        tmpfs_size="1g",
    ),
    ProfileName.PROJECT: SandboxProfile(
        name=ProfileName.PROJECT,
        network=True,
        mounts=[],  # Project dir mount added dynamically
        read_only_root=True,
        cpu_limit=2.0,
        memory_limit="2g",
        timeout_seconds=300,
        tmpfs_size="2g",
    ),
    ProfileName.FULL: SandboxProfile(
        name=ProfileName.FULL,
        network=True,
        mounts=[],  # Home dir mount added dynamically
        read_only_root=False,
        cpu_limit=4.0,
        memory_limit="4g",
        timeout_seconds=600,
        tmpfs_size="4g",
    ),
}


def get_profile(
    name: ProfileName | str,
    project_dir: str | None = None,
    home_dir: str | None = None,
) -> SandboxProfile:
    """Get a sandbox profile, optionally adding dynamic mounts."""
    if isinstance(name, str):
        name = ProfileName(name)

    profile = PROFILES[name].model_copy(deep=True)

    # Add /workspace tmpfs to all profiles
    profile.mounts.append({
        "source": "tmpfs",
        "target": "/workspace",
        "mode": "rw",
        "type": "tmpfs",
        "size": profile.tmpfs_size,
    })

    # Add project dir mount for project profile
    if name == ProfileName.PROJECT and project_dir:
        profile.mounts.append({
            "source": project_dir,
            "target": "/project",
            "mode": "ro",
            "type": "bind",
        })

    # Add home dir mount for full profile
    if name == ProfileName.FULL and home_dir:
        profile.mounts.append({
            "source": home_dir,
            "target": "/home/user",
            "mode": "rw",
            "type": "bind",
        })

    # Safety invariants — never allow these
    profile.privileged = False
    profile.docker_socket = False

    return profile
