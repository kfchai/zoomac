"""Phase 4 tests — Sandbox profiles, container config, and Docker manager."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memgate"))

from zoomac.sandbox.profiles import (
    PROFILES,
    ProfileName,
    SandboxProfile,
    get_profile,
)
from zoomac.sandbox.docker import (
    ContainerState,
    ContainerInfo,
    ExecutionResult,
    SandboxManager,
)


# --- Profile tests ---


def test_all_profiles_exist():
    """All four profiles are defined."""
    assert ProfileName.MINIMAL in PROFILES
    assert ProfileName.STANDARD in PROFILES
    assert ProfileName.PROJECT in PROFILES
    assert ProfileName.FULL in PROFILES


def test_minimal_profile():
    """Minimal profile has no network and no mounts."""
    p = get_profile(ProfileName.MINIMAL)
    assert p.network is False
    assert p.read_only_root is True
    assert p.cpu_limit == 1.0
    assert p.memory_limit == "512m"
    # Only tmpfs /workspace mount
    assert len(p.mounts) == 1
    assert p.mounts[0]["target"] == "/workspace"


def test_standard_profile():
    """Standard profile has network access."""
    p = get_profile(ProfileName.STANDARD)
    assert p.network is True
    assert p.read_only_root is True
    assert p.memory_limit == "1g"


def test_project_profile_with_dir():
    """Project profile mounts project directory read-only."""
    p = get_profile(ProfileName.PROJECT, project_dir="/home/user/myproject")
    assert p.network is True
    bind_mounts = [m for m in p.mounts if m.get("type") == "bind"]
    assert len(bind_mounts) == 1
    assert bind_mounts[0]["source"] == "/home/user/myproject"
    assert bind_mounts[0]["target"] == "/project"
    assert bind_mounts[0]["mode"] == "ro"


def test_project_profile_without_dir():
    """Project profile without dir has no bind mounts."""
    p = get_profile(ProfileName.PROJECT)
    bind_mounts = [m for m in p.mounts if m.get("type") == "bind"]
    assert len(bind_mounts) == 0


def test_full_profile_with_home():
    """Full profile mounts home directory read-write."""
    p = get_profile(ProfileName.FULL, home_dir="/home/user")
    assert p.network is True
    assert p.read_only_root is False
    assert p.memory_limit == "4g"
    bind_mounts = [m for m in p.mounts if m.get("type") == "bind"]
    assert len(bind_mounts) == 1
    assert bind_mounts[0]["source"] == "/home/user"
    assert bind_mounts[0]["mode"] == "rw"


def test_safety_invariants():
    """Privileged and docker_socket are always False."""
    for name in ProfileName:
        p = get_profile(name)
        assert p.privileged is False
        assert p.docker_socket is False


def test_profile_from_string():
    """Profiles can be loaded by string name."""
    p = get_profile("minimal")
    assert p.name == ProfileName.MINIMAL


def test_invalid_profile_raises():
    """Invalid profile name raises ValueError."""
    with pytest.raises(ValueError):
        get_profile("nonexistent")


def test_all_profiles_have_workspace_tmpfs():
    """Every profile gets a /workspace tmpfs mount."""
    for name in ProfileName:
        p = get_profile(name)
        tmpfs_mounts = [m for m in p.mounts if m.get("type") == "tmpfs"]
        assert len(tmpfs_mounts) == 1
        assert tmpfs_mounts[0]["target"] == "/workspace"


def test_profiles_are_deep_copied():
    """get_profile returns independent copies, not shared references."""
    p1 = get_profile(ProfileName.STANDARD)
    p2 = get_profile(ProfileName.STANDARD)
    p1.mounts.append({"source": "extra", "target": "/extra", "mode": "rw"})
    assert len(p2.mounts) != len(p1.mounts)


# --- SandboxManager config generation tests ---


def test_container_config_minimal():
    """Container config for minimal profile."""
    mgr = SandboxManager.__new__(SandboxManager)
    mgr._image = "zoomac-sandbox:latest"
    profile = get_profile(ProfileName.MINIMAL)
    config = mgr._build_container_config(profile)

    assert config["image"] == "zoomac-sandbox:latest"
    assert config["read_only"] is True
    assert config["network_mode"] == "none"
    assert config["mem_limit"] == "512m"
    assert config["privileged"] is False
    assert "no-new-privileges:true" in config["security_opt"]


def test_container_config_standard():
    """Container config for standard profile has network."""
    mgr = SandboxManager.__new__(SandboxManager)
    mgr._image = "zoomac-sandbox:latest"
    profile = get_profile(ProfileName.STANDARD)
    config = mgr._build_container_config(profile)

    assert config["network_mode"] == "bridge"
    assert config["mem_limit"] == "1g"


def test_container_config_project_mounts():
    """Container config for project profile includes bind mounts."""
    mgr = SandboxManager.__new__(SandboxManager)
    mgr._image = "zoomac-sandbox:latest"
    profile = get_profile(ProfileName.PROJECT, project_dir="/home/user/code")
    config = mgr._build_container_config(profile)

    assert "/home/user/code" in config.get("volumes", {})
    assert config["volumes"]["/home/user/code"]["mode"] == "ro"


def test_container_config_tmpfs():
    """Container config includes /workspace tmpfs."""
    mgr = SandboxManager.__new__(SandboxManager)
    mgr._image = "zoomac-sandbox:latest"
    profile = get_profile(ProfileName.MINIMAL)
    config = mgr._build_container_config(profile)

    assert "/workspace" in config.get("tmpfs", {})


def test_container_config_cpu_limit():
    """CPU limit is converted to nano_cpus."""
    mgr = SandboxManager.__new__(SandboxManager)
    mgr._image = "zoomac-sandbox:latest"
    profile = get_profile(ProfileName.FULL)
    config = mgr._build_container_config(profile)

    assert config["nano_cpus"] == int(4.0 * 1e9)


# --- Data model tests ---


def test_execution_result_defaults():
    """ExecutionResult has sensible defaults."""
    r = ExecutionResult(exit_code=0, stdout="hello", stderr="")
    assert r.timed_out is False
    assert r.duration_ms == 0


def test_container_info_defaults():
    """ContainerInfo initializes with correct defaults."""
    info = ContainerInfo(id="test123")
    assert info.state == ContainerState.IDLE
    assert info.container_id is None
    assert info.created_at is not None


def test_sandbox_manager_init():
    """SandboxManager initializes without connecting to Docker."""
    mgr = SandboxManager(
        image="test:latest",
        pool_size=2,
        project_dir="/tmp/project",
        home_dir="/home/user",
    )
    assert mgr._image == "test:latest"
    assert mgr._pool_size == 2
    assert mgr._client is None  # Lazy — no Docker connection yet
    assert len(mgr._containers) == 0


def test_sandbox_manager_list_containers_empty():
    """list_containers returns empty list initially."""
    mgr = SandboxManager()
    assert mgr.list_containers() == []
