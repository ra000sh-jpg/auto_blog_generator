import asyncio
import os
from dotenv import load_dotenv
from modules.llm.provider_factory import create_client

async def test_nvidia():
    load_dotenv()
    api_key = os.getenv("NVIDIA_API_KEY")
    if not api_key or "your_" in api_key:
        print("NVIDIA_API_KEY is not set correctly in .env file.")
        return

    print("Testing NVIDIA API (Llama 3.3 70B)...")
    try:
        client = create_client("nvidia", model="meta/llama-3.3-70b-instruct")
        response = await client.generate(
            system_prompt="You are a helpful assistant.",
            user_prompt="Hello, who are you? Please answer in Korean concisely."
        )
        print("\nResponse from NVIDIA:")
        print(f"Content: {response.content}")
        print(f"Model: {response.model}")
        print(f"Usage: {response.input_tokens} up, {response.output_tokens} down")
        await client.close()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_nvidia())
