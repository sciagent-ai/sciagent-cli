"""
State Management - Context window, todos, file persistence, and memory
"""
import os
import json
import hashlib
from datetime import datetime
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field, asdict
from pathlib import Path
from enum import Enum

from .llm import Message
from .defaults import DEFAULT_MODEL


class TodoStatus(Enum):
    PENDING = "☐"
    IN_PROGRESS = "◐"
    DONE = "☑"
    FAILED = "☒"


@dataclass
class TodoItem:
    """A single todo item"""
    description: str
    status: TodoStatus = TodoStatus.PENDING
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    completed_at: Optional[str] = None
    
    def mark_done(self):
        self.status = TodoStatus.DONE
        self.completed_at = datetime.now().isoformat()
    
    def mark_failed(self):
        self.status = TodoStatus.FAILED
        self.completed_at = datetime.now().isoformat()
    
    def mark_in_progress(self):
        self.status = TodoStatus.IN_PROGRESS
    
    def __str__(self):
        return f"{self.status.value} {self.description}"


@dataclass
class TodoList:
    """Manages the agent's task list"""
    items: List[TodoItem] = field(default_factory=list)
    
    def add(self, description: str) -> TodoItem:
        item = TodoItem(description=description)
        self.items.append(item)
        return item
    
    def get_pending(self) -> List[TodoItem]:
        return [i for i in self.items if i.status == TodoStatus.PENDING]
    
    def get_in_progress(self) -> List[TodoItem]:
        return [i for i in self.items if i.status == TodoStatus.IN_PROGRESS]
    
    def mark_done(self, index: int):
        if 0 <= index < len(self.items):
            self.items[index].mark_done()
    
    def mark_failed(self, index: int):
        if 0 <= index < len(self.items):
            self.items[index].mark_failed()
    
    def to_string(self) -> str:
        """Format for inclusion in context"""
        if not self.items:
            return "No todos defined."
        lines = ["Current Tasks:"]
        for i, item in enumerate(self.items):
            lines.append(f"  {i}. {item}")
        return "\n".join(lines)
    
    def to_dict(self) -> Dict:
        return {
            "items": [
                {
                    "description": item.description,
                    "status": item.status.name,
                    "created_at": item.created_at,
                    "completed_at": item.completed_at
                }
                for item in self.items
            ]
        }

    def sync_from_tool(self, todos: List[Dict]) -> None:
        """
        Sync state from todo tool output.

        The tool uses: content, status ("pending", "in_progress", "completed")
        We use: description, TodoStatus enum
        """
        # Map tool status strings to our enum
        status_map = {
            "pending": TodoStatus.PENDING,
            "in_progress": TodoStatus.IN_PROGRESS,
            "completed": TodoStatus.DONE,
        }

        # Replace items with synced data
        self.items = []
        for todo in todos:
            content = todo.get("content", "")
            status_str = todo.get("status", "pending")
            status = status_map.get(status_str, TodoStatus.PENDING)

            item = TodoItem(description=content, status=status)
            if status == TodoStatus.DONE:
                item.completed_at = datetime.now().isoformat()
            self.items.append(item)

    @classmethod
    def from_dict(cls, data: Dict) -> "TodoList":
        items = []
        for item_data in data.get("items", []):
            item = TodoItem(
                description=item_data["description"],
                status=TodoStatus[item_data["status"]],
                created_at=item_data.get("created_at", ""),
                completed_at=item_data.get("completed_at")
            )
            items.append(item)
        return cls(items=items)


@dataclass
class ContextWindow:
    """
    Manages the conversation context sent to the LLM
    
    Handles:
    - Message history
    - Context compression when approaching limits
    - System prompt management
    """
    system_prompt: str
    messages: List[Message] = field(default_factory=list)
    max_messages: int = 100  # Before compression
    
    def add_user_message(self, content: str) -> Message:
        msg = Message(role="user", content=content)
        self.messages.append(msg)
        return msg
    
    def add_assistant_message(self, content: str, tool_calls: List[Dict] = None) -> Message:
        msg = Message(role="assistant", content=content, tool_calls=tool_calls)
        self.messages.append(msg)
        return msg
    
    def add_tool_result(self, tool_call_id: str, tool_name: str, result: str) -> Message:
        msg = Message(
            role="tool",
            content=result,
            tool_call_id=tool_call_id,
            name=tool_name
        )
        self.messages.append(msg)
        return msg
    
    def get_messages(self) -> List[Message]:
        """Get all messages including system prompt"""
        return [Message(role="system", content=self.system_prompt)] + self.messages
    
    def compress_if_needed(self, summarizer=None):
        """
        Compress context if too many messages

        Strategy: Keep first message, summarize middle, keep recent
        CRITICAL: Never break tool_use/tool_result pairs (Anthropic API requirement)
        """
        if len(self.messages) <= self.max_messages:
            return

        # Keep first 5 and last 20 messages, but adjust to preserve tool pairs
        keep_start = 5
        keep_end = 20

        # Find safe cut points that don't orphan tool_use blocks
        start_idx = self._find_safe_cut_point(keep_start, forward=True)
        end_idx = len(self.messages) - self._find_safe_cut_point(keep_end, forward=False, from_end=True)

        if summarizer:
            # Use LLM to summarize middle section
            middle = self.messages[start_idx:end_idx]
            if middle:
                summary_text = summarizer(middle)
                summary_msg = Message(
                    role="assistant",
                    content=f"[Context Summary]\n{summary_text}"
                )
                self.messages = (
                    self.messages[:start_idx] +
                    [summary_msg] +
                    self.messages[end_idx:]
                )
        else:
            # Simple truncation at safe boundaries
            self.messages = self.messages[:start_idx] + self.messages[end_idx:]

    def _find_safe_cut_point(self, target_idx: int, forward: bool = True, from_end: bool = False) -> int:
        """
        Find a safe index to cut messages without orphaning tool_use/tool_result pairs.

        Args:
            target_idx: The target number of messages to keep
            forward: If True, search forward from target; if False, search backward
            from_end: If True, target_idx is counted from the end

        Returns:
            Safe index where cutting won't break tool pairs
        """
        if from_end:
            # Convert to index from start
            start_search = len(self.messages) - target_idx
        else:
            start_search = target_idx

        # Clamp to valid range
        start_search = max(0, min(start_search, len(self.messages)))

        # Check if we're in the middle of a tool_use/tool_result sequence
        # A tool_use (assistant with tool_calls) must be followed by tool_results

        if forward:
            idx = start_search
            while idx < len(self.messages):
                if self._is_safe_cut_point(idx):
                    return idx
                idx += 1
            return len(self.messages)
        else:
            idx = start_search
            while idx > 0:
                if self._is_safe_cut_point(idx):
                    return idx if from_end else len(self.messages) - idx
                idx -= 1
            return 0 if not from_end else len(self.messages)

    def _is_safe_cut_point(self, idx: int) -> bool:
        """
        Check if cutting at this index would orphan any tool_use blocks.

        Safe to cut if:
        - Previous message is not an assistant message with tool_calls, OR
        - Previous message's tool_calls all have corresponding tool_results before idx
        """
        if idx <= 0 or idx >= len(self.messages):
            return True

        # Look backward to find any assistant message with tool_calls
        # that might not have all its tool_results yet
        pending_tool_ids = set()

        for i in range(idx):
            msg = self.messages[i]

            # Track tool_calls from assistant messages
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    if tc_id:
                        pending_tool_ids.add(tc_id)

            # Remove tool_ids that have results
            if msg.role == "tool" and msg.tool_call_id:
                pending_tool_ids.discard(msg.tool_call_id)

        # Safe to cut if no pending tool_calls
        return len(pending_tool_ids) == 0
    
    def clear(self):
        """Clear all messages but keep system prompt"""
        self.messages = []

    def validate_and_repair(self) -> List[str]:
        """
        Validate message structure and repair if needed.

        Checks for orphaned tool_use blocks (Anthropic API requirement).
        Returns list of issues found/repaired.
        """
        issues = []
        pending_tool_calls = {}  # id -> (index, tool_call)

        i = 0
        while i < len(self.messages):
            msg = self.messages[i]

            # Track tool_calls from assistant messages
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    tc_name = (tc.get("function", {}).get("name", "unknown")
                               if isinstance(tc, dict) else getattr(tc, "name", "unknown"))
                    if tc_id:
                        pending_tool_calls[tc_id] = (i, tc_name)

            # Match tool results
            if msg.role == "tool" and msg.tool_call_id:
                if msg.tool_call_id in pending_tool_calls:
                    del pending_tool_calls[msg.tool_call_id]
                else:
                    # Orphaned tool_result without tool_call - remove it
                    issues.append(f"Removed orphaned tool_result at index {i}")
                    self.messages.pop(i)
                    continue  # Don't increment i

            i += 1

        # Handle any remaining pending tool_calls (missing results)
        for tc_id, (idx, tc_name) in pending_tool_calls.items():
            issues.append(f"Added missing tool_result for {tc_name} (id: {tc_id})")
            # Insert a placeholder result after the assistant message
            # Find the right position (after all existing tool results for that assistant msg)
            insert_pos = idx + 1
            while (insert_pos < len(self.messages) and
                   self.messages[insert_pos].role == "tool"):
                insert_pos += 1

            placeholder = Message(
                role="tool",
                content="[Tool execution result unavailable - context was repaired]",
                tool_call_id=tc_id,
                name=tc_name
            )
            self.messages.insert(insert_pos, placeholder)

        return issues

    def token_estimate(self) -> int:
        """Rough token estimate (4 chars ≈ 1 token)"""
        total_chars = len(self.system_prompt)
        for msg in self.messages:
            total_chars += len(msg.content or "")
        return total_chars // 4


@dataclass
class AgentState:
    """
    Complete state of an agent session

    Can be serialized/deserialized for persistence
    """
    session_id: str
    context: ContextWindow
    todos: TodoList
    working_dir: str
    model: str = DEFAULT_MODEL
    temperature: float = 0.0
    max_iterations: int = 120
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    
    def update(self):
        """Update timestamp"""
        self.updated_at = datetime.now().isoformat()
    
    def to_dict(self) -> Dict:
        return {
            "session_id": self.session_id,
            "system_prompt": self.context.system_prompt,
            "messages": [m.to_dict() for m in self.context.messages],
            "todos": self.todos.to_dict(),
            "working_dir": self.working_dir,
            "model": self.model,
            "temperature": self.temperature,
            "max_iterations": self.max_iterations,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> "AgentState":
        context = ContextWindow(
            system_prompt=data.get("system_prompt", ""),
            messages=[Message.from_dict(m) for m in data.get("messages", [])]
        )
        todos = TodoList.from_dict(data.get("todos", {}))

        return cls(
            session_id=data["session_id"],
            context=context,
            todos=todos,
            working_dir=data.get("working_dir", "."),
            model=data.get("model", DEFAULT_MODEL),
            temperature=data.get("temperature", 0.0),
            max_iterations=data.get("max_iterations", 120),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", "")
        )
    
    def save(self, path: Optional[str] = None):
        """Save state to file"""
        if path is None:
            path = f".agent_state_{self.session_id}.json"
        
        with open(path, 'w') as f:
            json.dump(self.to_dict(), f, indent=2)
    
    @classmethod
    def load(cls, path: str) -> "AgentState":
        """Load state from file"""
        with open(path) as f:
            return cls.from_dict(json.load(f))


class StateManager:
    """
    Manages state persistence and retrieval
    
    Supports:
    - File-based state storage
    - Session management
    - State checkpointing
    """
    
    def __init__(self, state_dir: str = ".agent_states"):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(exist_ok=True)
    
    def _state_path(self, session_id: str) -> Path:
        return self.state_dir / f"{session_id}.json"
    
    def save(self, state: AgentState):
        """Save agent state"""
        state.update()
        path = self._state_path(state.session_id)
        with open(path, 'w') as f:
            json.dump(state.to_dict(), f, indent=2)
    
    def load(self, session_id: str) -> Optional[AgentState]:
        """Load agent state by session ID"""
        path = self._state_path(session_id)
        if not path.exists():
            return None
        with open(path) as f:
            return AgentState.from_dict(json.load(f))
    
    def list_sessions(self) -> List[Dict]:
        """List all saved sessions"""
        sessions = []
        for path in self.state_dir.glob("*.json"):
            with open(path) as f:
                data = json.load(f)
                sessions.append({
                    "session_id": data["session_id"],
                    "created_at": data.get("created_at"),
                    "updated_at": data.get("updated_at"),
                    "task_count": len(data.get("todos", {}).get("items", []))
                })
        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
    
    def delete(self, session_id: str):
        """Delete a saved session"""
        path = self._state_path(session_id)
        if path.exists():
            path.unlink()
    
    def create_checkpoint(self, state: AgentState) -> str:
        """Create a checkpoint of current state"""
        checkpoint_id = f"{state.session_id}_checkpoint_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        checkpoint_path = self._state_path(checkpoint_id)
        with open(checkpoint_path, 'w') as f:
            json.dump(state.to_dict(), f, indent=2)
        return checkpoint_id


def generate_session_id(task: str = "") -> str:
    """Generate a unique session ID"""
    timestamp = datetime.now().isoformat()
    content = f"{timestamp}:{task}"
    return hashlib.sha256(content.encode()).hexdigest()[:12]
