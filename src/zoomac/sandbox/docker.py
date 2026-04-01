"""Docker container manager for sandboxed execution."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from zoomac.autonomy.pipeline import ApprovalDecision
from zoomac.sandbox.policy import SandboxExecutionIntent, SandboxPolicyResolver
from zoomac.sandbox.profiles import ProfileName, SandboxProfile, get_profile


class ContainerState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass
class ExecutionResult:
    """Result of running a command in a sandbox container."""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    duration_ms: int = 0
    container_id: str = ""


@dataclass
class ContainerInfo:
    """Tracks a managed container."""

    id: str
    container_id: str | None = None  # Docker container ID
    profile: SandboxProfile | None = None
    state: ContainerState = ContainerState.IDLE
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_used: datetime | None = None


class SandboxManager:
    """Manages a pool of Docker containers for sandboxed execution.

    Requires the `docker` Python SDK. All Docker operations are run in
    an executor to avoid blocking the async event loop.
    """

    DEFAULT_IMAGE = "zoomac-sandbox:latest"

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        pool_size: int = 3,
        project_dir: str | None = None,
        home_dir: str | None = None,
    ) -> None:
        self._image = image
        self._pool_size = pool_size
        self._project_dir = project_dir
        self._home_dir = home_dir
        self._client: Any | None = None  # docker.DockerClient (lazy)
        self._containers: dict[str, ContainerInfo] = {}
        self._policy = SandboxPolicyResolver(project_dir=project_dir, home_dir=home_dir)

    @property
    def client(self) -> Any:
        """Lazy-load Docker client."""
        if self._client is None:
            try:
                import docker
                self._client = docker.from_env()
            except ImportError:
                raise RuntimeError(
                    "Docker SDK not installed. Run: pip install docker"
                )
            except Exception as e:
                raise RuntimeError(f"Cannot connect to Docker daemon: {e}")
        return self._client

    def _build_container_config(self, profile: SandboxProfile) -> dict[str, Any]:
        """Convert a SandboxProfile into Docker container kwargs."""
        config: dict[str, Any] = {
            "image": self._image,
            "detach": True,
            "stdin_open": True,
            "tty": False,
            "read_only": profile.read_only_root,
            "network_mode": "bridge" if profile.network else "none",
            "mem_limit": profile.memory_limit,
            "nano_cpus": int(profile.cpu_limit * 1e9),
            "privileged": False,  # NEVER
            "security_opt": ["no-new-privileges:true"],
        }

        # Volume mounts
        volumes = {}
        tmpfs = {}
        for mount in profile.mounts:
            mount_type = mount.get("type", "bind")
            if mount_type == "tmpfs":
                tmpfs[mount["target"]] = f"size={mount.get('size', '1g')}"
            else:
                volumes[mount["source"]] = {
                    "bind": mount["target"],
                    "mode": mount.get("mode", "ro"),
                }

        if volumes:
            config["volumes"] = volumes
        if tmpfs:
            config["tmpfs"] = tmpfs

        return config

    async def execute(
        self,
        command: str,
        profile_name: ProfileName | str = ProfileName.STANDARD,
        timeout: int | None = None,
    ) -> ExecutionResult:
        """Execute a command in a sandboxed container.

        Creates a new container, runs the command, captures output, and removes it.
        """
        profile = get_profile(
            profile_name,
            project_dir=self._project_dir,
            home_dir=self._home_dir,
        )
        effective_timeout = timeout or profile.timeout_seconds

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None,
            self._execute_sync,
            command,
            profile,
            effective_timeout,
        )

    def resolve_execution_policy(
        self,
        command: str,
        *,
        requested_profile: ProfileName | str | None = None,
        requires_network: bool = False,
        reads_project: bool = False,
        writes_project: bool = False,
        allowed_paths: list[str] | None = None,
        approval: ApprovalDecision | None = None,
    ):
        """Resolve the effective sandbox policy for a command."""
        intent = SandboxExecutionIntent(
            command_text=command,
            requested_profile=requested_profile,
            requires_network=requires_network,
            reads_project=reads_project,
            writes_project=writes_project,
            allowed_paths=allowed_paths or [],
        )
        return self._policy.resolve(intent, approval=approval)

    def _execute_sync(
        self,
        command: str,
        profile: SandboxProfile,
        timeout: int,
    ) -> ExecutionResult:
        """Synchronous container execution (run in executor)."""
        import time

        container_config = self._build_container_config(profile)
        container = None
        cid = uuid.uuid4().hex[:12]
        info = ContainerInfo(id=cid, profile=profile, state=ContainerState.RUNNING)
        self._containers[cid] = info

        start_time = time.monotonic()
        try:
            container = self.client.containers.run(
                command=["/bin/sh", "-c", command],
                **container_config,
            )
            info.container_id = container.id

            # Wait for completion with timeout
            try:
                result = container.wait(timeout=timeout)
                exit_code = result.get("StatusCode", -1)
                timed_out = False
            except Exception:
                # Timeout or other error — kill the container
                try:
                    container.kill()
                except Exception:
                    pass
                exit_code = -1
                timed_out = True

            # Capture logs
            try:
                stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
                stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
            except Exception:
                stdout = ""
                stderr = ""

            duration_ms = int((time.monotonic() - start_time) * 1000)
            info.state = ContainerState.STOPPED

            return ExecutionResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
                duration_ms=duration_ms,
                container_id=container.id if container else "",
            )

        except Exception as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            info.state = ContainerState.ERROR
            return ExecutionResult(
                exit_code=-1,
                stdout="",
                stderr=str(e),
                timed_out=False,
                duration_ms=duration_ms,
            )

        finally:
            # Always clean up
            if container:
                try:
                    container.remove(force=True)
                except Exception:
                    pass
            self._containers.pop(cid, None)

    def list_containers(self) -> list[ContainerInfo]:
        """List currently tracked containers."""
        return list(self._containers.values())

    async def cleanup(self) -> int:
        """Force-remove all tracked containers."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._cleanup_sync)

    def _cleanup_sync(self) -> int:
        """Synchronous cleanup."""
        removed = 0
        for info in list(self._containers.values()):
            if info.container_id:
                try:
                    container = self.client.containers.get(info.container_id)
                    container.remove(force=True)
                    removed += 1
                except Exception:
                    pass
        self._containers.clear()
        return removed

    def image_exists(self) -> bool:
        """Check if the sandbox base image exists locally."""
        try:
            self.client.images.get(self._image)
            return True
        except Exception:
            return False

    async def build_image(self, dockerfile_path: str = ".") -> bool:
        """Build the sandbox base image from a Dockerfile."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._build_image_sync, dockerfile_path
        )

    def _build_image_sync(self, dockerfile_path: str) -> bool:
        """Synchronous image build."""
        try:
            self.client.images.build(
                path=dockerfile_path,
                tag=self._image,
                rm=True,
            )
            return True
        except Exception:
            return False
