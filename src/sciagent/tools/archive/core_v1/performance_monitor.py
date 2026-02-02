"""
Performance monitoring and optimization tool.

This tool provides real-time performance monitoring, benchmarking capabilities,
and optimization suggestions for the sciagent system. It tracks tool execution
times, memory usage, and provides recommendations for improving performance.
"""

from __future__ import annotations

import os
import time
import psutil
import threading
from typing import Dict, Any, Optional, List, Callable
from datetime import datetime, timedelta
import json
from pathlib import Path
from collections import defaultdict, deque

from sciagent.base_tool import BaseTool


class PerformanceMonitorTool(BaseTool):
    """Monitor and optimize sciagent performance."""

    name = "performance_monitor"
    description = "Monitor system performance, benchmark operations, and provide optimization recommendations"
    input_schema: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "enum": [
                    "start_monitoring", "stop_monitoring", "get_stats", "benchmark",
                    "optimize_recommendations", "memory_profile", "tool_analysis",
                    "export_metrics", "reset_stats", "set_alerts"
                ],
                "description": "The performance operation to execute"
            },
            "duration": {"type": "integer", "description": "Monitoring duration in seconds", "default": 60},
            "operation": {"type": "string", "description": "Operation to benchmark"},
            "iterations": {"type": "integer", "description": "Number of iterations for benchmarks", "default": 10},
            "export_format": {"type": "string", "enum": ["json", "csv"], "default": "json"},
            "alert_thresholds": {
                "type": "object",
                "properties": {
                    "cpu_percent": {"type": "number", "default": 80.0},
                    "memory_percent": {"type": "number", "default": 85.0},
                    "tool_timeout": {"type": "number", "default": 30.0}
                }
            }
        },
        "required": ["command"]
    }

    def __init__(self):
        super().__init__()
        self._monitoring = False
        self._monitor_thread = None
        self._stats = {
            "start_time": None,
            "end_time": None,
            "tool_executions": defaultdict(list),
            "system_metrics": deque(maxlen=1000),
            "memory_snapshots": [],
            "performance_alerts": [],
            "optimization_events": []
        }
        self._alert_thresholds = {
            "cpu_percent": 80.0,
            "memory_percent": 85.0,
            "tool_timeout": 30.0
        }

    def _get_system_metrics(self) -> Dict[str, Any]:
        """Get current system performance metrics."""
        try:
            cpu_percent = psutil.cpu_percent(interval=0.1)
            memory = psutil.virtual_memory()
            disk = psutil.disk_usage('/')
            
            # Process-specific metrics
            process = psutil.Process()
            process_memory = process.memory_info()
            process_cpu = process.cpu_percent()
            
            return {
                "timestamp": datetime.now().isoformat(),
                "system": {
                    "cpu_percent": cpu_percent,
                    "memory_percent": memory.percent,
                    "memory_used_gb": memory.used / (1024**3),
                    "memory_total_gb": memory.total / (1024**3),
                    "disk_percent": disk.percent,
                    "disk_used_gb": disk.used / (1024**3)
                },
                "process": {
                    "memory_rss_mb": process_memory.rss / (1024**2),
                    "memory_vms_mb": process_memory.vms / (1024**2),
                    "cpu_percent": process_cpu,
                    "num_threads": process.num_threads(),
                    "open_files": len(process.open_files()),
                    "connections": len(process.connections())
                }
            }
        except Exception as e:
            return {"error": str(e)}

    def _monitor_performance(self):
        """Background monitoring thread."""
        while self._monitoring:
            metrics = self._get_system_metrics()
            if "error" not in metrics:
                self._stats["system_metrics"].append(metrics)
                
                # Check alert thresholds
                self._check_alerts(metrics)
            
            time.sleep(1)  # Monitor every second

    def _check_alerts(self, metrics: Dict[str, Any]):
        """Check if metrics exceed alert thresholds."""
        alerts = []
        
        system = metrics.get("system", {})
        process = metrics.get("process", {})
        
        if system.get("cpu_percent", 0) > self._alert_thresholds["cpu_percent"]:
            alerts.append({
                "type": "high_cpu",
                "value": system["cpu_percent"],
                "threshold": self._alert_thresholds["cpu_percent"],
                "timestamp": metrics["timestamp"]
            })
        
        if system.get("memory_percent", 0) > self._alert_thresholds["memory_percent"]:
            alerts.append({
                "type": "high_memory",
                "value": system["memory_percent"],
                "threshold": self._alert_thresholds["memory_percent"],
                "timestamp": metrics["timestamp"]
            })
        
        if alerts:
            self._stats["performance_alerts"].extend(alerts)

    def _start_monitoring(self, duration: Optional[int] = None) -> Dict[str, Any]:
        """Start performance monitoring."""
        if self._monitoring:
            return {"success": False, "error": "Monitoring already active"}
        
        self._monitoring = True
        self._stats["start_time"] = datetime.now().isoformat()
        self._stats["end_time"] = None
        
        # Start monitoring thread
        self._monitor_thread = threading.Thread(target=self._monitor_performance, daemon=True)
        self._monitor_thread.start()
        
        # Schedule auto-stop if duration specified
        if duration:
            def auto_stop():
                time.sleep(duration)
                if self._monitoring:
                    self.stop_monitoring()
            
            threading.Thread(target=auto_stop, daemon=True).start()
        
        return {
            "success": True,
            "output": f"Performance monitoring started{f' for {duration}s' if duration else ''}",
            "start_time": self._stats["start_time"]
        }

    def _stop_monitoring(self) -> Dict[str, Any]:
        """Stop performance monitoring."""
        if not self._monitoring:
            return {"success": False, "error": "Monitoring not active"}
        
        self._monitoring = False
        self._stats["end_time"] = datetime.now().isoformat()
        
        if self._monitor_thread:
            self._monitor_thread.join(timeout=2)
        
        return {
            "success": True,
            "output": "Performance monitoring stopped",
            "end_time": self._stats["end_time"],
            "metrics_collected": len(self._stats["system_metrics"])
        }

    def _get_performance_stats(self) -> Dict[str, Any]:
        """Get comprehensive performance statistics."""
        if not self._stats["system_metrics"]:
            return {"success": False, "error": "No performance data available"}
        
        metrics = list(self._stats["system_metrics"])
        
        # Calculate averages and peaks
        cpu_values = [m["system"]["cpu_percent"] for m in metrics if "system" in m]
        memory_values = [m["system"]["memory_percent"] for m in metrics if "system" in m]
        process_memory = [m["process"]["memory_rss_mb"] for m in metrics if "process" in m]
        
        summary = {
            "monitoring_period": {
                "start": self._stats["start_time"],
                "end": self._stats["end_time"] or datetime.now().isoformat(),
                "duration_seconds": len(metrics)
            },
            "system_performance": {
                "cpu": {
                    "average": sum(cpu_values) / len(cpu_values) if cpu_values else 0,
                    "peak": max(cpu_values) if cpu_values else 0,
                    "minimum": min(cpu_values) if cpu_values else 0
                },
                "memory": {
                    "average": sum(memory_values) / len(memory_values) if memory_values else 0,
                    "peak": max(memory_values) if memory_values else 0,
                    "minimum": min(memory_values) if memory_values else 0
                },
                "process_memory_mb": {
                    "average": sum(process_memory) / len(process_memory) if process_memory else 0,
                    "peak": max(process_memory) if process_memory else 0,
                    "growth": process_memory[-1] - process_memory[0] if len(process_memory) > 1 else 0
                }
            },
            "tool_performance": self._analyze_tool_performance(),
            "alerts": {
                "total_alerts": len(self._stats["performance_alerts"]),
                "alert_types": list(set(alert["type"] for alert in self._stats["performance_alerts"])),
                "recent_alerts": self._stats["performance_alerts"][-5:]
            }
        }
        
        return {"success": True, "output": "Performance statistics generated", "stats": summary}

    def _analyze_tool_performance(self) -> Dict[str, Any]:
        """Analyze tool execution performance."""
        tool_stats = {}
        
        for tool_name, executions in self._stats["tool_executions"].items():
            if executions:
                durations = [exec_data["duration"] for exec_data in executions if "duration" in exec_data]
                
                tool_stats[tool_name] = {
                    "execution_count": len(executions),
                    "average_duration": sum(durations) / len(durations) if durations else 0,
                    "max_duration": max(durations) if durations else 0,
                    "min_duration": min(durations) if durations else 0,
                    "success_rate": sum(1 for exec_data in executions if exec_data.get("success")) / len(executions),
                    "recent_executions": executions[-3:]
                }
        
        return tool_stats

    def _benchmark_operation(self, operation: str, iterations: int) -> Dict[str, Any]:
        """Benchmark a specific operation."""
        results = []
        
        for i in range(iterations):
            start_time = time.time()
            start_memory = psutil.Process().memory_info().rss
            
            # Simulate operation (in real implementation, this would call actual operations)
            if operation == "file_read":
                # Benchmark file reading
                test_file = Path(__file__)
                if test_file.exists():
                    with open(test_file, 'r') as f:
                        content = f.read()
            elif operation == "llm_call_simulation":
                # Simulate LLM call overhead
                time.sleep(0.1)  # Simulate network latency
            elif operation == "memory_allocation":
                # Benchmark memory allocation
                large_list = [i for i in range(10000)]
                del large_list
            
            end_time = time.time()
            end_memory = psutil.Process().memory_info().rss
            
            results.append({
                "iteration": i + 1,
                "duration": end_time - start_time,
                "memory_delta": end_memory - start_memory,
                "timestamp": datetime.now().isoformat()
            })
        
        # Calculate statistics
        durations = [r["duration"] for r in results]
        memory_deltas = [r["memory_delta"] for r in results]
        
        return {
            "success": True,
            "output": f"Benchmark completed: {operation} ({iterations} iterations)",
            "benchmark_results": {
                "operation": operation,
                "iterations": iterations,
                "duration_stats": {
                    "average": sum(durations) / len(durations),
                    "min": min(durations),
                    "max": max(durations),
                    "total": sum(durations)
                },
                "memory_stats": {
                    "average_delta": sum(memory_deltas) / len(memory_deltas),
                    "max_delta": max(memory_deltas),
                    "total_delta": sum(memory_deltas)
                },
                "raw_results": results
            }
        }

    def _get_optimization_recommendations(self) -> Dict[str, Any]:
        """Generate performance optimization recommendations."""
        recommendations = []
        
        if not self._stats["system_metrics"]:
            return {"success": False, "error": "No performance data for recommendations"}
        
        # Analyze recent metrics
        recent_metrics = list(self._stats["system_metrics"])[-60:]  # Last 60 seconds
        
        if recent_metrics:
            avg_cpu = sum(m["system"]["cpu_percent"] for m in recent_metrics) / len(recent_metrics)
            avg_memory = sum(m["system"]["memory_percent"] for m in recent_metrics) / len(recent_metrics)
            process_memory_growth = recent_metrics[-1]["process"]["memory_rss_mb"] - recent_metrics[0]["process"]["memory_rss_mb"]
            
            # CPU recommendations
            if avg_cpu > 70:
                recommendations.append({
                    "type": "cpu_optimization",
                    "severity": "high" if avg_cpu > 90 else "medium",
                    "description": f"High CPU usage detected ({avg_cpu:.1f}%)",
                    "suggestions": [
                        "Enable fast mode (--no-skills) for simple operations",
                        "Use tool execution parallelization",
                        "Consider reducing LiteLLM overhead with direct API calls"
                    ]
                })
            
            # Memory recommendations
            if avg_memory > 80 or process_memory_growth > 100:  # 100MB growth
                recommendations.append({
                    "type": "memory_optimization", 
                    "severity": "high" if avg_memory > 90 else "medium",
                    "description": f"High memory usage detected ({avg_memory:.1f}%, growth: {process_memory_growth:.1f}MB)",
                    "suggestions": [
                        "Enable conversation compression more frequently",
                        "Clear unused skill cache with clear_unloaded_skills()",
                        "Use lazy loading for large skills",
                        "Check for memory leaks in long-running operations"
                    ]
                })
        
        # Tool performance recommendations
        tool_stats = self._analyze_tool_performance()
        slow_tools = [name for name, stats in tool_stats.items() if stats["average_duration"] > 5.0]
        
        if slow_tools:
            recommendations.append({
                "type": "tool_optimization",
                "severity": "medium",
                "description": f"Slow tools detected: {', '.join(slow_tools)}",
                "suggestions": [
                    "Cache tool results where possible",
                    "Use timeout settings for long-running operations",
                    "Consider async execution for independent tools"
                ]
            })
        
        return {
            "success": True,
            "output": f"Generated {len(recommendations)} optimization recommendations",
            "recommendations": recommendations,
            "analysis_period": len(recent_metrics),
            "next_check": (datetime.now() + timedelta(minutes=5)).isoformat()
        }

    def _export_metrics(self, format_type: str = "json") -> Dict[str, Any]:
        """Export performance metrics to file."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        export_data = {
            "metadata": {
                "export_time": datetime.now().isoformat(),
                "monitoring_start": self._stats["start_time"],
                "monitoring_end": self._stats["end_time"],
                "total_metrics": len(self._stats["system_metrics"])
            },
            "system_metrics": list(self._stats["system_metrics"]),
            "tool_performance": self._analyze_tool_performance(),
            "alerts": self._stats["performance_alerts"]
        }
        
        filename = f"performance_metrics_{timestamp}.{format_type}"
        
        try:
            if format_type == "json":
                with open(filename, 'w') as f:
                    json.dump(export_data, f, indent=2, default=str)
            elif format_type == "csv":
                import pandas as pd
                # Convert system metrics to DataFrame and export
                df = pd.DataFrame(list(self._stats["system_metrics"]))
                df.to_csv(filename, index=False)
            
            return {
                "success": True,
                "output": f"Metrics exported to {filename}",
                "filename": filename,
                "format": format_type,
                "records_exported": len(self._stats["system_metrics"])
            }
        
        except Exception as e:
            return {"success": False, "error": f"Export failed: {str(e)}"}

    def track_tool_execution(self, tool_name: str, start_time: float, end_time: float, success: bool, **metadata):
        """Track tool execution for performance analysis (called by agent)."""
        execution_data = {
            "tool_name": tool_name,
            "start_time": start_time,
            "end_time": end_time,
            "duration": end_time - start_time,
            "success": success,
            "timestamp": datetime.now().isoformat(),
            **metadata
        }
        
        self._stats["tool_executions"][tool_name].append(execution_data)

    def run(self, tool_input: Dict[str, Any], agent: Optional[Any] = None) -> Dict[str, Any]:
        command = tool_input.get("command")
        
        try:
            if command == "start_monitoring":
                duration = tool_input.get("duration")
                return self._start_monitoring(duration)
            
            elif command == "stop_monitoring":
                return self._stop_monitoring()
            
            elif command == "get_stats":
                return self._get_performance_stats()
            
            elif command == "benchmark":
                operation = tool_input.get("operation", "file_read")
                iterations = tool_input.get("iterations", 10)
                return self._benchmark_operation(operation, iterations)
            
            elif command == "optimize_recommendations":
                return self._get_optimization_recommendations()
            
            elif command == "memory_profile":
                metrics = self._get_system_metrics()
                return {
                    "success": True,
                    "output": "Memory profile captured",
                    "memory_profile": metrics
                }
            
            elif command == "export_metrics":
                format_type = tool_input.get("export_format", "json")
                return self._export_metrics(format_type)
            
            elif command == "reset_stats":
                self._stats = {
                    "start_time": None,
                    "end_time": None,
                    "tool_executions": defaultdict(list),
                    "system_metrics": deque(maxlen=1000),
                    "memory_snapshots": [],
                    "performance_alerts": [],
                    "optimization_events": []
                }
                return {"success": True, "output": "Performance statistics reset"}
            
            elif command == "set_alerts":
                thresholds = tool_input.get("alert_thresholds", {})
                self._alert_thresholds.update(thresholds)
                return {
                    "success": True,
                    "output": "Alert thresholds updated",
                    "current_thresholds": self._alert_thresholds
                }
            
            else:
                return {"success": False, "error": f"Unknown command: {command}"}
        
        except Exception as e:
            return {"success": False, "error": str(e)}


def get_tool() -> BaseTool:
    """Return an instance of PerformanceMonitorTool."""
    return PerformanceMonitorTool()