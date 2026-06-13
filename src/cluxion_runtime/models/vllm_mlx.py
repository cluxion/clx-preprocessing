"""vLLM-MLX runtime profile for Apple Silicon."""

from __future__ import annotations

import platform
from dataclasses import dataclass

from cluxion_runtime.core.types import ModelRuntimeProfile, RuntimeKind


@dataclass(frozen=True)
class VllmMlxProfile:
    """vLLM-MLX OpenAI-compatible server launch options."""

    model: str
    host: str = "127.0.0.1"
    port: int = 8000
    max_tokens: int = 128_000
    continuous_batching: bool = True
    prefix_cache: bool = True
    executable: str = "vllm-mlx"

    @property
    def base_url(self) -> str:
        """OpenAI-compatible base URL."""
        return f"http://{self.host}:{self.port}/v1"

    def command(self) -> tuple[str, ...]:
        """Build the vLLM-MLX server start command."""
        cmd = [
            self.executable,
            "serve",
            self.model,
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--max-tokens",
            str(self.max_tokens),
        ]
        if self.continuous_batching:
            cmd.append("--continuous-batching")
        if self.prefix_cache:
            cmd.append("--enable-prefix-cache")
        return tuple(cmd)

    def runtime_profile(self) -> ModelRuntimeProfile:
        """Convert to the model runtime profile used by the shared harness."""
        return ModelRuntimeProfile(
            kind=RuntimeKind.VLLM_MLX,
            model=self.model,
            base_url=self.base_url,
            command=self.command(),
        )


def build_vllm_mlx_profile(
    model: str,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    max_tokens: int = 128_000,
    executable: str = "vllm-mlx",
) -> ModelRuntimeProfile:
    """Build a vLLM-MLX profile for an explicit model ID."""
    return VllmMlxProfile(
        model=model,
        host=host,
        port=port,
        max_tokens=max_tokens,
        executable=executable,
    ).runtime_profile()


def select_mac_local_profile(model: str, *, port: int = 8000) -> ModelRuntimeProfile:
    """Prefer vLLM-MLX on Mac Apple Silicon; otherwise use a generic OpenAI endpoint."""
    if platform.system() == "Darwin" and platform.machine().lower() in {"arm64", "aarch64"}:
        return build_vllm_mlx_profile(model, port=port)
    return ModelRuntimeProfile(
        kind=RuntimeKind.OPENAI_COMPAT,
        model=model,
        base_url=f"http://127.0.0.1:{port}/v1",
        command=("vllm", "serve", model, "--host", "127.0.0.1", "--port", str(port)),
    )


__all__ = ["VllmMlxProfile", "build_vllm_mlx_profile", "select_mac_local_profile"]
