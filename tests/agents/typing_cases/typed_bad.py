"""Must fail mypy: the output is an Invoice, not a str."""

from nexura_agents import Agent, FakeModel
from pydantic import BaseModel


class Invoice(BaseModel):
    number: str


agent = Agent(name="a", instructions="x", model=FakeModel(responses=["{}"]), output_type=Invoice)
wrong: str = agent.run("hi").output
