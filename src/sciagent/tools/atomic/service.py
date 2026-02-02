"""
Service tool - run code in containerized simulation environments.

Provides a generic interface for running code in Docker containers
for various simulation tools (RCWA, MEEP, OpenFOAM, etc.)

Resolution order for images:
1. Check local Docker images
2. Pull from GHCR (ghcr.io/sciagent-ai/{name}:latest)
3. Build from Dockerfile (services/{name}/Dockerfile)
"""

from __future__ import annotations

import subprocess
import os
import yaml
import tempfile
import json
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass


@dataclass
class ToolResult:
    """Result from tool execution."""
    success: bool
    output: Any
    error: Optional[str] = None


class ServiceTool:
    """Run code/commands in containerized simulation services.

    Actions:
    - list: Show available services from registry
    - status: Check which services are installed locally
    - ensure: Pull/build a service without running
    - run: Execute code/command in a service container
    - info: Get detailed info about a service
    """

    name = "service"
    description = """Run code in containerized simulation services (RCWA, MEEP, OpenFOAM, etc.).

Actions:
- list: Show available services
- status: Check which are installed locally
- ensure: Pull/build service image
- run: Execute code in service container
- info: Get service details and examples

Examples:
- List services: action="list"
- Check status: action="status"
- Run RCWA simulation: action="run", service="rcwa", code="import S4; print(S4.__version__)"
- Run shell command: action="run", service="rcwa", command="python3 --version"
"""

    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "status", "ensure", "run", "info"],
                "description": "Action to perform"
            },
            "service": {
                "type": "string",
                "description": "Service name (e.g., 'rcwa', 'meep', 'openfoam'). Required for run/ensure/info."
            },
            "code": {
                "type": "string",
                "description": "Python/script code to execute in the container. Used with action='run'."
            },
            "command": {
                "type": "string",
                "description": "Shell command to execute in the container. Used with action='run'. If both code and command provided, code takes precedence."
            },
            "files": {
                "type": "object",
                "description": "Files to mount into container. Keys are container paths, values are local paths or content strings."
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (default: 300)",
                "default": 300
            }
        },
        "required": ["action"]
    }

    def __init__(self, working_dir: str = "."):
        self.working_dir = Path(working_dir).resolve()
        self._registry_path = self._find_registry()
        self._registry_cache = None

    def _find_registry(self) -> Optional[Path]:
        """Find services/registry.yaml relative to project root."""
        # Search up from working_dir to find services/registry.yaml
        search_paths = [
            self.working_dir / "services" / "registry.yaml",
            self.working_dir.parent / "services" / "registry.yaml",
            Path(__file__).parent.parent.parent / "services" / "registry.yaml",
        ]

        for path in search_paths:
            if path.exists():
                return path

        return None

    def _load_registry(self) -> Dict[str, Any]:
        """Load service registry from YAML."""
        if self._registry_cache:
            return self._registry_cache

        if not self._registry_path or not self._registry_path.exists():
            return {"services": {}, "defaults": {}}

        with open(self._registry_path) as f:
            self._registry_cache = yaml.safe_load(f)

        return self._registry_cache

    def _docker_available(self) -> tuple[bool, str]:
        """Check if Docker is available and running."""
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                return True, "Docker is running"
            else:
                return False, f"Docker not running: {result.stderr.strip()}"
        except FileNotFoundError:
            return False, "Docker is not installed"
        except subprocess.TimeoutExpired:
            return False, "Docker command timed out"
        except Exception as e:
            return False, f"Docker check failed: {e}"

    def _get_local_images(self) -> Dict[str, str]:
        """Get locally available Docker images."""
        try:
            result = subprocess.run(
                ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                images = {}
                for line in result.stdout.strip().split('\n'):
                    if line:
                        images[line] = line
                return images
            return {}
        except Exception:
            return {}

    def _image_exists_locally(self, image: str) -> bool:
        """Check if a Docker image exists locally."""
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True,
                text=True,
                timeout=30
            )
            return result.returncode == 0
        except Exception:
            return False

    def _pull_image(self, image: str) -> tuple[bool, str]:
        """Pull an image from registry."""
        try:
            result = subprocess.run(
                ["docker", "pull", image],
                capture_output=True,
                text=True,
                timeout=600  # 10 min for large images
            )
            if result.returncode == 0:
                return True, f"Successfully pulled {image}"
            else:
                return False, f"Pull failed: {result.stderr.strip()}"
        except subprocess.TimeoutExpired:
            return False, "Pull timed out (>10 min)"
        except Exception as e:
            return False, f"Pull error: {e}"

    def _build_image(self, service_name: str, dockerfile_path: str, tag: str) -> tuple[bool, str]:
        """Build an image from Dockerfile."""
        dockerfile = Path(dockerfile_path)
        if not dockerfile.exists():
            return False, f"Dockerfile not found: {dockerfile_path}"

        try:
            result = subprocess.run(
                ["docker", "build", "-t", tag, "-f", str(dockerfile), str(dockerfile.parent)],
                capture_output=True,
                text=True,
                timeout=1800,  # 30 min for builds
                cwd=self.working_dir
            )
            if result.returncode == 0:
                return True, f"Successfully built {tag}"
            else:
                # Return last 30 lines of error
                error_lines = result.stderr.strip().split('\n')[-30:]
                return False, f"Build failed:\n" + '\n'.join(error_lines)
        except subprocess.TimeoutExpired:
            return False, "Build timed out (>30 min)"
        except Exception as e:
            return False, f"Build error: {e}"

    def _resolve_image(self, service_name: str, service_config: Dict) -> tuple[bool, str, str]:
        """
        Resolve image for a service. Returns (success, image_name, message).

        Resolution order:
        1. Check local images
        2. Pull from GHCR
        3. Build from Dockerfile
        """
        image = service_config.get("image", f"ghcr.io/sciagent-ai/{service_name}")
        tag = image if ":" in image else f"{image}:latest"

        # 1. Check local
        if self._image_exists_locally(tag):
            return True, tag, f"Using local image: {tag}"

        # Also check without registry prefix (local build name)
        local_tag = f"{service_name}:latest"
        if self._image_exists_locally(local_tag):
            return True, local_tag, f"Using local image: {local_tag}"

        # 2. Try pulling from GHCR
        success, msg = self._pull_image(tag)
        if success:
            return True, tag, msg

        # 3. Try building from Dockerfile
        dockerfile = service_config.get("dockerfile")
        if dockerfile:
            # Resolve dockerfile path relative to registry location
            if self._registry_path:
                base_path = self._registry_path.parent.parent
                dockerfile_full = base_path / dockerfile
            else:
                dockerfile_full = self.working_dir / dockerfile

            success, msg = self._build_image(service_name, str(dockerfile_full), local_tag)
            if success:
                return True, local_tag, msg
            else:
                return False, "", f"Could not pull or build image for {service_name}:\nPull: {tag} not found\nBuild: {msg}"

        return False, "", f"Image {tag} not found and no Dockerfile configured"

    def _run_in_container(
        self,
        image: str,
        service_config: Dict,
        code: Optional[str] = None,
        command: Optional[str] = None,
        files: Optional[Dict[str, str]] = None,
        timeout: int = 300
    ) -> tuple[bool, str]:
        """Run code or command in a container."""
        runtime = service_config.get("runtime", "python3")
        workdir = service_config.get("workdir", "/workspace")

        docker_cmd = [
            "docker", "run", "--rm",
            "-w", workdir,
        ]

        # Mount working directory
        docker_cmd.extend(["-v", f"{self.working_dir}:/workspace"])

        # Handle file mounts
        if files:
            for container_path, local_path_or_content in files.items():
                if os.path.exists(local_path_or_content):
                    # It's a file path, mount it
                    docker_cmd.extend(["-v", f"{local_path_or_content}:{container_path}:ro"])
                else:
                    # It's content, write to temp file and mount
                    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.py') as f:
                        f.write(local_path_or_content)
                        docker_cmd.extend(["-v", f"{f.name}:{container_path}:ro"])

        docker_cmd.append(image)

        # Determine what to run
        if code:
            # Run Python/script code
            if runtime == "python3" or runtime == "python":
                docker_cmd.extend(["python3", "-c", code])
            else:
                # For other runtimes, write code to temp and execute
                docker_cmd.extend(["bash", "-c", code])
        elif command:
            docker_cmd.extend(["bash", "-c", command])
        else:
            # Interactive mode / default command
            docker_cmd.extend([runtime])

        try:
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self.working_dir
            )

            output = ""
            if result.stdout:
                output += result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}" if output else result.stderr

            if result.returncode == 0:
                return True, output.strip() if output else "(no output)"
            else:
                return False, f"Exit code {result.returncode}:\n{output.strip()}"

        except subprocess.TimeoutExpired:
            return False, f"Execution timed out after {timeout}s"
        except Exception as e:
            return False, f"Execution error: {e}"

    def execute(
        self,
        action: str,
        service: Optional[str] = None,
        code: Optional[str] = None,
        command: Optional[str] = None,
        files: Optional[Dict[str, str]] = None,
        timeout: int = 300
    ) -> ToolResult:
        """Execute service tool action."""

        # Check Docker availability first
        docker_ok, docker_msg = self._docker_available()
        if not docker_ok and action not in ["list", "info"]:
            return ToolResult(
                success=False,
                output=None,
                error=f"Docker required but not available: {docker_msg}\n\nPlease start Docker Desktop or install Docker."
            )

        registry = self._load_registry()
        services = registry.get("services", {})

        # === LIST ===
        if action == "list":
            if not services:
                return ToolResult(
                    success=True,
                    output="No services configured. Add services to services/registry.yaml"
                )

            lines = ["Available services:\n"]
            for name, config in services.items():
                desc = config.get("description", "No description")
                lines.append(f"  {name}: {desc}")

            return ToolResult(success=True, output='\n'.join(lines))

        # === STATUS ===
        if action == "status":
            local_images = self._get_local_images()

            lines = ["Service Status:\n"]
            for name, config in services.items():
                image = config.get("image", f"ghcr.io/sciagent-ai/{name}")
                tag = image if ":" in image else f"{image}:latest"
                local_tag = f"{name}:latest"

                if tag in local_images or local_tag in local_images:
                    status = "✓ installed"
                else:
                    status = "✗ not installed"

                lines.append(f"  {name}: {status}")

            return ToolResult(success=True, output='\n'.join(lines))

        # === INFO ===
        if action == "info":
            if not service:
                return ToolResult(success=False, output=None, error="Service name required for 'info' action")

            if service not in services:
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Unknown service: {service}. Available: {list(services.keys())}"
                )

            config = services[service]
            info = [
                f"Service: {service}",
                f"Description: {config.get('description', 'N/A')}",
                f"Image: {config.get('image', 'N/A')}",
                f"Runtime: {config.get('runtime', 'python3')}",
                f"Dockerfile: {config.get('dockerfile', 'N/A')}",
            ]

            if config.get("capabilities"):
                info.append("\nCapabilities:")
                for cap in config["capabilities"]:
                    info.append(f"  - {cap}")

            if config.get("example"):
                info.append(f"\nExample:\n```\n{config['example'].strip()}\n```")

            return ToolResult(success=True, output='\n'.join(info))

        # === ENSURE ===
        if action == "ensure":
            if not service:
                return ToolResult(success=False, output=None, error="Service name required for 'ensure' action")

            if service not in services:
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Unknown service: {service}. Available: {list(services.keys())}"
                )

            config = services[service]
            success, image, msg = self._resolve_image(service, config)

            return ToolResult(success=success, output=msg if success else None, error=None if success else msg)

        # === RUN ===
        if action == "run":
            if not service:
                return ToolResult(success=False, output=None, error="Service name required for 'run' action")

            if service not in services:
                return ToolResult(
                    success=False,
                    output=None,
                    error=f"Unknown service: {service}. Available: {list(services.keys())}"
                )

            if not code and not command:
                return ToolResult(
                    success=False,
                    output=None,
                    error="Either 'code' or 'command' required for 'run' action"
                )

            config = services[service]

            # Ensure image is available
            success, image, msg = self._resolve_image(service, config)
            if not success:
                return ToolResult(success=False, output=None, error=msg)

            # Run in container
            success, output = self._run_in_container(
                image=image,
                service_config=config,
                code=code,
                command=command,
                files=files,
                timeout=timeout
            )

            return ToolResult(
                success=success,
                output=output if success else None,
                error=None if success else output
            )

        return ToolResult(
            success=False,
            output=None,
            error=f"Unknown action: {action}. Valid actions: list, status, ensure, run, info"
        )

    def to_schema(self) -> Dict:
        """Convert to OpenAI-style tool schema."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters
        }


def get_tool(working_dir: str = ".") -> ServiceTool:
    """Factory function for tool discovery."""
    return ServiceTool(working_dir)


# For testing
if __name__ == "__main__":
    import sys

    # Find project root (where services/ is)
    working_dir = Path(__file__).parent.parent.parent
    tool = ServiceTool(str(working_dir))

    print("=== SERVICE TOOL TEST ===\n")

    # Test list
    print("1. List services:")
    result = tool.execute(action="list")
    print(result.output)
    print()

    # Test status
    print("2. Service status:")
    result = tool.execute(action="status")
    print(result.output)
    print()

    # Test info
    print("3. RCWA info:")
    result = tool.execute(action="info", service="rcwa")
    print(result.output)
    print()

    # Test run (if rcwa is available)
    print("4. Run S4 test code:")
    result = tool.execute(
        action="run",
        service="rcwa",
        code="import S4; print('S4 imported successfully'); S = S4.New(Lattice=1, NumBasis=5); print('Created S4 simulation object')"
    )
    if result.success:
        print(result.output)
    else:
        print(f"Error: {result.error}")
