"""The few ElevenLabs REST calls the generator needs, with the standard library only.

Routes and fields follow the official Python SDK (elevenlabs-python, Oct 2026). Simulations run through agent
tests: the older ``/simulate-conversation`` route is deprecated and removed on 31 Oct 2026.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

BASE = os.environ.get("ELEVENLABS_BASE_URL", "https://api.elevenlabs.io")


class ElevenLabsError(RuntimeError):
    pass


class ElevenLabs:
    def __init__(self, api_key: str | None = None, base: str = BASE):
        self.api_key = api_key or os.environ.get("ELEVENLABS_API_KEY")
        if not self.api_key:
            raise ElevenLabsError("set ELEVENLABS_API_KEY")
        self.base = base.rstrip("/")

    def _request(self, method: str, path: str, body: dict | None = None, raw: bool = False, retries: int = 4):
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(retries + 1):
            req = urllib.request.Request(f"{self.base}/{path}", data=data, method=method,
                                         headers={"xi-api-key": self.api_key, "content-type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    payload = resp.read()
                    return payload if raw else (json.loads(payload) if payload else {})
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:500]
                if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise ElevenLabsError(f"{method} {path}: HTTP {e.code}: {detail}") from None
            except urllib.error.URLError as e:
                if attempt < retries:
                    time.sleep(2 ** (attempt + 1))
                    continue
                raise ElevenLabsError(f"{method} {path}: {e.reason}") from None

    # -- agents ---------------------------------------------------------------------------------------------

    def create_agent(self, name: str, prompt: str, first_message: str, language: str, llm: str,
                     placeholders: dict[str, str]) -> str:
        body = {"name": name, "conversation_config": {"agent": {
            "first_message": first_message, "language": language,
            "dynamic_variables": {"dynamic_variable_placeholders": placeholders},
            "prompt": {"prompt": prompt, "llm": llm}}}}
        return self._request("POST", "v1/convai/agents/create", body)["agent_id"]

    # -- simulation tests -----------------------------------------------------------------------------------

    def create_simulation_test(self, name: str, scenario: str, conditions: list[str], dynamic_variables: dict,
                               max_turns: int, user_llm: str | None = None) -> str:
        body = {"type": "simulation", "name": name, "simulation_scenario": scenario,
                "simulation_max_turns": max_turns, "success_conditions": conditions,
                "dynamic_variables": dynamic_variables}
        if user_llm:
            body["simulated_user_model"] = user_llm
        return self._request("POST", "v1/convai/agent-testing/create", body)["id"]

    def delete_test(self, test_id: str) -> None:
        self._request("DELETE", f"v1/convai/agent-testing/{test_id}")

    def run_tests(self, agent_id: str, test_ids: list[str], name: str) -> str:
        body = {"name": name, "tests": [{"test_id": t} for t in test_ids]}
        return self._request("POST", f"v1/convai/agents/{agent_id}/run-tests", body)["id"]

    def get_invocation(self, invocation_id: str) -> dict:
        return self._request("GET", f"v1/convai/test-invocations/{invocation_id}")

    def wait_invocation(self, invocation_id: str, timeout: float = 900, poll: float = 5) -> dict:
        """The invocation once no run is pending, or as it stands at ``timeout``."""

        deadline = time.time() + timeout
        while True:
            inv = self.get_invocation(invocation_id)
            if all(r.get("status") != "pending" for r in inv.get("test_runs", [])) or time.time() > deadline:
                return inv
            time.sleep(poll)

    # -- text to speech -------------------------------------------------------------------------------------

    def tts(self, voice_id: str, text: str, model_id: str = "eleven_multilingual_v2") -> bytes:
        return self._request("POST", f"v1/text-to-speech/{voice_id}?output_format=mp3_44100_128",
                             {"text": text, "model_id": model_id}, raw=True)
