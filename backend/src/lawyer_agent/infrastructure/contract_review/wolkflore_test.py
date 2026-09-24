import asyncio
import random
from workflows import Workflow, Context, step
from workflows.events import Event, StartEvent, StopEvent

from llama_index.utils.workflow import (
    draw_all_possible_flows,
    draw_most_recent_execution,
)

# Draw all
draw_all_possible_flows(MyWorkflow, filename="all_paths.html")

# Draw an execution
w = MyWorkflow()
handler = w.run(topic="Pirates")
# 使用 asyncio.run() 简化异步调用，无需手动 await
asyncio.run(handler)
draw_most_recent_execution(handler, filename="most_recent.html")

import asyncio
from workflows import Workflow, step
from workflows.events import StartEvent, StopEvent
from workflows.server import WorkflowServer


class MyWorkflow(Workflow):
    @step
    async def my_step(self, ev: StartEvent) -> StopEvent:
        return StopEvent(result="Done!")


async def main():
    server = WorkflowServer()
    server.add_workflow("my_workflow", MyWorkflow())
    await server.serve("0.0.0.0", "8080")


if __name__ == "__main__":
    asyncio.run(main())