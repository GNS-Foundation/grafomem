"""GRAFOMEM governance hooks for AutoGen.

Provides message-level governance for AutoGen conversations.
Evaluates GRAFOMEM policies before messages are processed
and logs decisions to the audit trail.

Usage::

    from grafomem import GrafomemClient
    from grafomem.autogen import GrafomemGovernanceHook

    client = GrafomemClient(api_key="gfm_...")
    hook = GrafomemGovernanceHook(client=client)

    # Register with an AutoGen agent
    agent.register_hook("process_message_before_send", hook.pre_send)
"""
from __future__ import annotations

import logging
import time
from typing import Any

from grafomem.client import GrafomemClient

logger = logging.getLogger("grafomem.autogen.governance")


class GrafomemGovernanceHook:
    """AutoGen message hook with GRAFOMEM governance.

    Provides pre-send and post-receive hooks that evaluate GRAFOMEM
    governance policies on every message in an AutoGen conversation.

    Args:
        client: A GrafomemClient instance.
        action: The governance action to evaluate (default "inference").
        block_on_deny: If True (default), denied messages return None
            (blocking the send). If False, messages are allowed through
            with a warning log.
    """

    def __init__(
        self,
        client: GrafomemClient,
        *,
        action: str = "inference",
        block_on_deny: bool = True,
    ) -> None:
        self._client = client
        self._action = action
        self._block_on_deny = block_on_deny

    def pre_send(
        self,
        sender: str,
        message: str | dict,
        recipient: str,
        **kwargs: Any,
    ) -> str | dict | None:
        """Pre-send governance hook.

        Returns the message unchanged if allowed, or None if denied
        (when block_on_deny=True).
        """
        content = message if isinstance(message, str) else str(message)
        context = {
            "sender": sender,
            "recipient": recipient,
            "content_length": len(content),
            "source": "autogen",
            "timestamp": time.time(),
        }
        try:
            result = self._client.governance.evaluate(self._action, context)
            verdict = result.get("verdict", "allow")
            if verdict == "deny":
                logger.warning(
                    "Governance denied message from %s to %s: %s",
                    sender, recipient, result.get("reason", "policy denied"),
                )
                if self._block_on_deny:
                    return None
            logger.info("Governance: %s for %s → %s", verdict, sender, recipient)
        except Exception as e:
            logger.warning("Governance check failed (allowing): %s", e)

        return message

    def post_receive(
        self,
        sender: str,
        message: str | dict,
        **kwargs: Any,
    ) -> None:
        """Post-receive decision logging."""
        content = message if isinstance(message, str) else str(message)
        try:
            self._client.decisions.log(
                action=self._action,
                output=content[:2000],
                source="autogen",
                meta={"sender": sender, "type": "autogen_message"},
            )
        except Exception as e:
            logger.warning("Decision logging failed: %s", e)
