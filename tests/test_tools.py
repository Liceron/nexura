"""Tests for tool definition, schema generation, argument validation, and execution."""

import pytest
from pydantic import BaseModel, Field

from nexura_runtime import (
    Tool,
    ToolExecutionError,
    ToolResultSerializationError,
    ToolValidationError,
    tool,
)


class Address(BaseModel):
    street: str
    city: str
    pincode: int = Field(..., description="Postal PIN code")


class OrderInput(BaseModel):
    order_id: str
    destination: Address
    priority: bool = False


@pytest.mark.asyncio
async def test_tool_schema_generation() -> None:
    @tool
    def search_items(query: str, limit: int = 10) -> str:
        """Search available inventory items."""
        return f"Found items for {query}"

    schema = search_items.to_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "search_items"
    assert schema["function"]["description"] == "Search available inventory items."
    parameters = schema["function"]["parameters"]
    assert "query" in parameters["properties"]
    assert "limit" in parameters["properties"]
    assert "query" in parameters.get("required", [])


@pytest.mark.asyncio
async def test_tool_with_nested_pydantic_schema() -> None:
    called = False

    @tool
    def process_order(order: OrderInput) -> dict[str, str]:
        """Process a structured order with nested address."""
        nonlocal called
        called = True
        return {
            "status": "confirmed",
            "city": order.destination.city,
            "order_id": order.order_id,
        }

    schema = process_order.to_schema()
    properties = schema["function"]["parameters"]["properties"]
    assert "order" in properties

    # Valid invocation with nested fields
    valid_args = {
        "order": {
            "order_id": "ORD-555",
            "destination": {
                "street": "MG Road",
                "city": "Bengaluru",
                "pincode": 560001,
            },
            "priority": True,
        }
    }
    result = await process_order.execute(valid_args)
    assert called is True
    assert '"status": "confirmed"' in result
    assert '"city": "Bengaluru"' in result


@pytest.mark.asyncio
async def test_invalid_input_causes_zero_tool_effects() -> None:
    side_effects_count = 0

    @tool
    def dangerous_action(amount: int, target: str) -> str:
        """Simulate a side-effectful tool."""
        nonlocal side_effects_count
        side_effects_count += 1
        return f"Transferred {amount} to {target}"

    # Pass invalid argument (string instead of int for amount)
    invalid_args = {"amount": "not_a_number", "target": "user123"}

    with pytest.raises(ToolValidationError) as exc_info:
        await dangerous_action.execute(invalid_args)

    assert exc_info.value.tool_name == "dangerous_action"
    assert side_effects_count == 0, "Tool body must NOT execute on invalid inputs!"


@pytest.mark.asyncio
async def test_sync_and_async_tool_execution() -> None:
    @tool
    def sync_tool(x: int, y: int) -> int:
        return x + y

    @tool
    async def async_tool(text: str) -> str:
        return f"async: {text.upper()}"

    res_sync = await sync_tool.execute({"x": 15, "y": 25})
    assert res_sync == "40"

    res_async = await async_tool.execute({"text": "hello world"})
    assert res_async == "async: HELLO WORLD"


@pytest.mark.asyncio
async def test_tool_execution_error_preserves_cause() -> None:
    @tool
    def failing_tool() -> None:
        raise ValueError("Database connection lost unexpectedly")

    with pytest.raises(ToolExecutionError) as exc_info:
        await failing_tool.execute({})

    assert exc_info.value.tool_name == "failing_tool"
    assert "Database connection lost unexpectedly" in str(exc_info.value)
    assert isinstance(exc_info.value.original_error, ValueError)
    assert exc_info.value.__cause__ is exc_info.value.original_error


@pytest.mark.asyncio
async def test_tool_result_serialization_error() -> None:
    class UnserializableObject:
        pass

    @tool
    def bad_result_tool() -> object:
        return UnserializableObject()

    with pytest.raises(ToolResultSerializationError) as exc_info:
        await bad_result_tool.execute({})

    assert exc_info.value.tool_name == "bad_result_tool"


@pytest.mark.asyncio
async def test_tool_preserves_unicode_without_escape() -> None:
    indic_text = "धन्यवाद, आपकी सहायता के लिए! (Thank you for your help)"

    @tool
    def unicode_tool() -> dict[str, str]:
        return {"greeting": indic_text}

    serialized = await unicode_tool.execute({})
    assert indic_text in serialized
    assert "\\u0927" not in serialized  # ensure_ascii=False checked


def test_tool_direct_instantiation() -> None:
    def add_numbers(a: int, b: int) -> int:
        return a + b

    t = Tool("custom_add", add_numbers, description="Add two integers")
    assert t.name == "custom_add"
    assert t.description == "Add two integers"
    assert not t.is_async
