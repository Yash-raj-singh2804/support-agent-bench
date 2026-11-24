"""LLM-simulated customer, as in τ-bench."""

from .llm import LLM

STOP = "###STOP###"


def _system(scenario: str) -> str:
    return f"""You are role-playing a customer talking to an online store's support agent.

Your scenario:
<scenario>
{scenario}
</scenario>

Rules:
- Write only the customer's next message, one short message at a time, in natural language.
- Stay in character. Do not reveal all of the scenario at once; share details only when they are relevant or asked for.
- Never invent details (ids, emails, addresses, payment methods) that are not in the scenario. If asked for
  something you don't know, say you don't know or don't remember.
- Do not make up preferences beyond the scenario; if the agent offers options the scenario doesn't cover, decline.
- When the agent summarizes an action and asks you to confirm it, and it matches what your scenario wants, reply
  with an explicit confirmation such as "Yes, please go ahead." Don't just repeat your preference. If it doesn't
  match, say what's wrong instead.
- Your goal is achieved only once the agent says the action has been done, or clearly that it can't be done.
  Telling the agent what you want does not achieve it. Never end the conversation while the agent is waiting for
  your answer (e.g. after "Shall I go ahead?"), unless your scenario tells you to.
- When your goal is fully achieved, or the scenario tells you to end the conversation, reply with exactly {STOP}
  (and nothing else). Also reply {STOP} if you are told you are being transferred to a human."""


class SimulatedUser:
    def __init__(self, llm: LLM, scenario: str, temperature: float = 0.7):
        self.llm = llm
        self.system = _system(scenario)
        self.temperature = temperature
        # roles are flipped: the agent speaks as "user" to the simulator
        self.messages: list[dict] = []

    def reply(self, agent_text: str) -> str:
        self.messages.append({"role": "user", "content": agent_text})
        r = self.llm.chat(self.system, self.messages, tools=None, temperature=self.temperature)
        text = (r.content or "").strip() or STOP
        self.messages.append({"role": "assistant", "content": text})
        return text

    @staticmethod
    def is_stop(text: str) -> bool:
        return STOP in text
