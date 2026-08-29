"""READ-ONLY helper: print the message list held for one LangGraph session."""
import asyncio, os, sys
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

DSN = (
    f"postgresql://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
    f"@{os.environ['POSTGRES_HOST']}:{os.environ['POSTGRES_PORT']}/{os.environ['POSTGRES_DB']}"
)

async def main():
    async with AsyncPostgresSaver.from_conn_string(DSN) as saver:
        cp = await saver.aget({"configurable": {"thread_id": sys.argv[1]}})
        if not cp:
            return
        for m in cp["channel_values"].get("messages", []):
            c = getattr(m, "content", "")
            if isinstance(c, list):
                c = " ".join(str(x) for x in c)
            c = " ".join(str(c).split())
            if c:
                print(f"{type(m).__name__}: {c}")

asyncio.run(main())
