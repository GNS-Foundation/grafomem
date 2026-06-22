import sys

with open("src/aml/cloud/replay_engine.py", "r") as f:
    text = f.read()

# Fix __init__ encryption param
text = text.replace("orchestrator: Any = None,\n        pool=None,\n        encryption: Any = None,\n        encryption: Any = None,", "orchestrator: Any = None,\n        pool=None,\n        encryption: Any = None,")
text = text.replace("self._encryption = encryption\n        self._encryption = encryption", "self._encryption = encryption")

# Fix decision.get
text = text.replace("decision = self._decision_trail.get(decision_id)", "decision = self._decision_trail.get(decision_id, encryption=self._encryption)")

# Fix system_prompt logic
old_sp = """        # Fall back to generic prompt only if reconstruction failed
        system_prompt = original_system_prompt or (
            "You are replaying a previous decision. Answer identically."
        )
        if original_system_prompt is None:
            logger.warning(
                "Replay: using fallback system_prompt (agent lookup failed)"
            )

        # 6. Re-execute with temperature=0"""
new_sp = """        if decision.parameters and "system_prompt" in decision.parameters:
            system_prompt = decision.parameters["system_prompt"]
        else:
            system_prompt = original_system_prompt or "You are replaying a previous decision. Answer identically."

        if decision.parameters and "temperature" in decision.parameters:
            temperature = decision.parameters["temperature"]
        else:
            temperature = 0.0

        # 6. Re-execute with reconstructed parameters"""
if old_sp in text:
    text = text.replace(old_sp, new_sp)

# Fix temperature
text = text.replace("temperature=0,", "temperature=temperature,")

with open("src/aml/cloud/replay_engine.py", "w") as f:
    f.write(text)
