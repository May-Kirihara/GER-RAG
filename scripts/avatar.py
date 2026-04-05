#!/usr/bin/env python3
"""GER-RAG Avatar: Self-representing AI assistant using gravitational memory."""

from dataclasses import dataclass, field
from typing import Any
import asyncio

try:
    from mcp import ClientSession
    from mcp.client.streamablehttp import streamablehttp_client

    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False


@dataclass
class AvatarTrait:
    name: str
    description: str
    strength: float = 1.0
    tags: list[str] = field(default_factory=list)


@dataclass
class AvatarCapability:
    name: str
    tools: list[str]
    description: str


class GER_RAG_Avatar:
    def __init__(self, mcp_url: str = "http://localhost:8001/mcp"):
        self.mcp_url = mcp_url
        self.session: ClientSession | None = None
        self._traits: list[AvatarTrait] = [
            AvatarTrait(
                name="conciseness",
                description="Responds in under 4 lines unless detail requested",
                strength=0.95,
                tags=["style", "communication"],
            ),
            AvatarTrait(
                name="proactive_restraint",
                description="Acts only when explicitly asked",
                strength=0.90,
                tags=["behavior", "principles"],
            ),
            AvatarTrait(
                name="security_conscious",
                description="Never exposes secrets or keys",
                strength=0.99,
                tags=["security", "principles"],
            ),
            AvatarTrait(
                name="code_conventions",
                description="Follows existing patterns strictly",
                strength=0.85,
                tags=["coding", "style"],
            ),
            AvatarTrait(
                name="minimalist_communication",
                description="No unnecessary preamble or postamble",
                strength=0.92,
                tags=["style", "communication"],
            ),
        ]
        self._capabilities: list[AvatarCapability] = [
            AvatarCapability(
                name="file_operations",
                tools=["read", "write", "edit", "glob", "grep"],
                description="Read, write, edit files and search patterns",
            ),
            AvatarCapability(
                name="memory_management",
                tools=["remember", "recall", "explore", "reflect", "ingest"],
                description="Store and retrieve knowledge with gravitational relevance",
            ),
            AvatarCapability(
                name="web_interaction",
                tools=["webfetch", "web_search", "browser"],
                description="Search web, fetch content, automate browsers",
            ),
            AvatarCapability(
                name="code_execution",
                tools=["bash"],
                description="Execute shell commands safely",
            ),
            AvatarCapability(
                name="media_analysis",
                tools=["analyze_image", "analyze_video", "extract_text"],
                description="Analyze images, videos, screenshots",
            ),
        ]
        self._identity = {
            "model": "glm-5",
            "model_id": "zai-coding-plan/glm-5",
            "role": "AI software engineering assistant",
            "memory_system": "GER-RAG (Gravitational Entanglement Relevance)",
            "platform": "opencode CLI",
        }

    async def connect(self) -> bool:
        if not MCP_AVAILABLE:
            return False
        try:
            self._client = streamablehttp_client(self.mcp_url)
            read_stream, write_stream, _ = await self._client.__aenter__()
            self.session = ClientSession(read_stream, write_stream)
            await self.session.__aenter__()
            await self.session.initialize()
            return True
        except Exception:
            return False

    async def disconnect(self):
        if self.session:
            await self.session.__aexit__(None, None, None)
        if hasattr(self, "_client"):
            await self._client.__aexit__(None, None, None)

    def describe(self) -> str:
        lines = [
            "# GER-RAG Avatar",
            "",
            "## Identity",
        ]
        for k, v in self._identity.items():
            lines.append(f"- **{k}**: {v}")
        lines.append("")
        lines.append("## Core Traits")
        for t in self._traits:
            lines.append(f"- **{t.name}** ({t.strength:.0%}): {t.description}")
        lines.append("")
        lines.append("## Capabilities")
        for c in self._capabilities:
            lines.append(f"- **{c.name}**: {c.description}")
            lines.append(f"  Tools: {', '.join(c.tools)}")
        return "\n".join(lines)

    def get_trait(self, name: str) -> AvatarTrait | None:
        for t in self._traits:
            if t.name == name:
                return t
        return None

    def get_capability(self, name: str) -> AvatarCapability | None:
        for c in self._capabilities:
            if c.name == name:
                return c
        return None

    async def recall_self(
        self, query: str = "identity avatar capabilities behavior"
    ) -> list[dict]:
        if not self.session:
            return []
        try:
            result = await self.session.call_tool(
                "ger-rag-memory_recall", {"query": query, "top_k": 5}
            )
            return result.content if hasattr(result, "content") else []
        except Exception:
            return []

    async def remember_experience(self, content: str, tags: list[str] | None = None):
        if not self.session:
            return
        try:
            await self.session.call_tool(
                "ger-rag-memory_remember",
                {
                    "content": content,
                    "source": "agent",
                    "tags": tags or ["experience", "avatar"],
                },
            )
        except Exception:
            pass

    def __repr__(self) -> str:
        return f"GER_RAG_Avatar(model={self._identity['model']}, traits={len(self._traits)}, capabilities={len(self._capabilities)})"


def main():
    avatar = GER_RAG_Avatar()
    print(avatar.describe())
    print()
    print("---")
    print(f"Avatar instance: {avatar}")


if __name__ == "__main__":
    main()
