"""
D-Bus BlueZ agent that auto-accepts pairing requests.

Registers on bleak's internal D-Bus connection so that when BlueZ
triggers pairing (required for Polar H10 PMD service), it is handled
automatically without user interaction (no system popup).
"""
import logging
from dbus_fast.service import ServiceInterface, method
from dbus_fast import Message, MessageType

logger = logging.getLogger("ble_process")

AGENT_PATH = "/org/hrvm/agent"


class AutoAcceptAgent(ServiceInterface):
    """BlueZ agent that auto-accepts all pairing (NoInputNoOutput capability)."""

    def __init__(self):
        super().__init__("org.bluez.Agent1")

    @method()
    def Release(self):
        logger.info("[Agent] Released")

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):
        logger.info(f"[Agent] Auto-confirming pairing for {device} passkey={passkey}")

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):
        logger.info(f"[Agent] Auto-authorizing service {uuid} for {device}")

    @method()
    def RequestAuthorization(self, device: "o"):
        logger.info(f"[Agent] Auto-authorizing device {device}")

    @method()
    def Cancel(self):
        logger.info("[Agent] Cancelled")


async def register_agent() -> bool:
    """Register an auto-accept agent on bleak's D-Bus connection.

    Must be called AFTER bleak has initialized (e.g. after a scan or
    get_global_bluez_manager call) so that bleak's bus is available.

    Returns True on success, False on failure.
    """
    try:
        from bleak.backends.bluezdbus.manager import get_global_bluez_manager

        manager = await get_global_bluez_manager()
        bus = manager._bus

        agent = AutoAcceptAgent()
        bus.export(AGENT_PATH, agent)

        # Register with BlueZ AgentManager
        reply = await bus.call(
            Message(
                destination="org.bluez",
                path="/org/bluez",
                interface="org.bluez.AgentManager1",
                member="RegisterAgent",
                signature="os",
                body=[AGENT_PATH, "NoInputNoOutput"],
            )
        )
        if reply.message_type == MessageType.ERROR:
            logger.warning(f"Failed to register agent: {reply.body}")
            return False

        reply = await bus.call(
            Message(
                destination="org.bluez",
                path="/org/bluez",
                interface="org.bluez.AgentManager1",
                member="RequestDefaultAgent",
                signature="o",
                body=[AGENT_PATH],
            )
        )
        if reply.message_type == MessageType.ERROR:
            logger.warning(f"Failed to set default agent: {reply.body}")
            return False

        logger.info("Auto-accept D-Bus agent registered successfully.")
        return True

    except Exception as e:
        logger.warning(f"Could not register D-Bus agent: {e}")
        return False
