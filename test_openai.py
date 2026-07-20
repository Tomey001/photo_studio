from openai import OpenAI
from dotenv import load_dotenv
import os

load_dotenv()

client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY")
)

try:
    response = client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=[
            {
                "role": "user",
                "content": "Give me one photography tip"
            }
        ]
    )

    print(response.choices[0].message.content)

except Exception as e:
    print(type(e).__name__)
    print(e)