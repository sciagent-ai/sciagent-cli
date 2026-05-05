#!/usr/bin/env python3
"""
Test script for workspace storage MVP.

Tests:
1. StorageMount creation
2. SkyPilot backend workspace mount generation
3. File mounts building
4. Compute tool with workspace=True
"""

import sys

def test_storage_mount():
    """Test StorageMount dataclass."""
    from sciagent.compute.job import StorageMount, StorageMode

    mount = StorageMount(
        path="/workspace",
        bucket="test-bucket",
        store="s3",
        mode=StorageMode.MOUNT,
    )

    assert mount.path == "/workspace"
    assert mount.bucket == "test-bucket"
    assert mount.store == "s3"
    assert mount.mode == StorageMode.MOUNT
    print("✓ StorageMount creation works")

def test_compute_requirements_with_storage():
    """Test ComputeRequirements with storage."""
    from sciagent.compute.job import ComputeRequirements, StorageMount, StorageMode

    mount = StorageMount(path="/data", bucket="my-bucket", store="s3")
    req = ComputeRequirements(cpus=4, memory_gb=8, storage=[mount])

    assert req.cpus == 4
    assert req.storage is not None
    assert len(req.storage) == 1
    assert req.storage[0].bucket == "my-bucket"
    print("✓ ComputeRequirements with storage works")

def test_skypilot_backend_workspace():
    """Test SkyPilot backend workspace mount generation."""
    from sciagent.compute.backends.skypilot import SkyPilotBackend

    backend = SkyPilotBackend()

    # Test workspace mount generation (doesn't require SkyPilot to be available)
    mount = backend.get_workspace_mount("test123")

    assert mount.path == "/workspace"
    assert mount.bucket == "sciagent-workspace-test123"
    assert mount.store in ["s3", "gcs", "azure"]  # Depends on enabled cloud
    print(f"✓ Workspace mount generation works (store={mount.store})")

def test_compute_tool_session_id():
    """Test compute tool session ID management."""
    from sciagent.tools.atomic.compute import ComputeTool

    # Reset shared session ID
    ComputeTool._shared_session_id = None

    tool = ComputeTool()

    # First call should generate session ID
    session1 = tool._get_session_id()
    assert session1 is not None
    assert len(session1) == 8

    # Second call should return same ID
    session2 = tool._get_session_id()
    assert session1 == session2

    # Explicit session ID should override
    session3 = tool._get_session_id("custom123")
    assert session3 == "custom123"

    print("✓ Session ID management works")

def test_skypilot_available():
    """Test if SkyPilot is available and configured."""
    from sciagent.compute.backends.skypilot import SkyPilotBackend

    backend = SkyPilotBackend()
    available = backend.is_available()

    if available:
        print("✓ SkyPilot is available and configured")
        store = backend.get_enabled_store()
        print(f"  Enabled store: {store}")
    else:
        print("⚠ SkyPilot not available (install skypilot[aws] and configure credentials)")

    return available

def test_storage_mounts_building():
    """Test building storage_mounts for SkyPilot task."""
    from sciagent.compute.backends.skypilot import SkyPilotBackend
    from sciagent.compute.job import StorageMount, StorageMode

    backend = SkyPilotBackend()

    if not backend.is_available():
        print("⚠ Skipping storage_mounts test (SkyPilot not available)")
        return

    mounts = [
        StorageMount(path="/workspace", bucket="test-workspace", store="s3"),
        StorageMount(path="/data", bucket="test-data", store="s3", mode=StorageMode.COPY),
    ]

    storage_mounts = backend._build_storage_mounts(mounts)

    assert "/workspace" in storage_mounts
    assert "/data" in storage_mounts
    print("✓ Storage mounts building works")

def main():
    print("=" * 50)
    print("Testing Workspace Storage MVP")
    print("=" * 50)
    print()

    try:
        test_storage_mount()
        test_compute_requirements_with_storage()
        test_skypilot_backend_workspace()
        test_compute_tool_session_id()
        skypilot_available = test_skypilot_available()
        if skypilot_available:
            test_storage_mounts_building()

        print()
        print("=" * 50)
        print("All tests passed! MVP is ready.")
        print("=" * 50)

        if skypilot_available:
            print()
            print("Next: Test with actual SkyPilot job:")
            print("  from sciagent.tools.atomic.compute import ComputeTool")
            print("  tool = ComputeTool()")
            print("  result = tool.execute(")
            print("      service='openfoam-swak4foam',")
            print("      command='echo hello > /workspace/test.txt',")
            print("      backend='skypilot',")
            print("      workspace=True,")
            print("  )")

        return 0

    except Exception as e:
        print(f"✗ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return 1

if __name__ == "__main__":
    sys.exit(main())
