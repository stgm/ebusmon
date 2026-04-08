import asyncio
from pyebus import Ebus

HOST = "192.168.178.27"
PORT = 8888
TARGET = "buildingcircuitflow"

async def main():
    ebus = Ebus(HOST, port=PORT)
    await ebus.async_load_msgdefs()

    all_msgdefs = list(ebus.msgdefs)
    print(f"Total msgdefs loaded: {len(all_msgdefs)}\n")

    matches = [m for m in all_msgdefs if TARGET in m.name.lower()]
    if not matches:
        print(f"No msgdef found matching '{TARGET}'")
        print("\nAll available msgdef names:")
        for m in sorted(all_msgdefs, key=lambda m: m.name):
            print(f"  {m.circuit}/{m.name}")
        return

    for msgdef in matches:
        print(f"=== {msgdef.circuit}/{msgdef.name} ===")
        print(f"  comment : {getattr(msgdef, 'comment', None)}")
        for fielddef in msgdef.fields:
            print(f"  field   : {fielddef.name!r}  unit={fielddef.unit!r}  type={type(fielddef).__name__}")
        try:
            values = await ebus.async_read(msgdef, ttl=60)
            print(f"  live    : {values}")
        except Exception as e:
            print(f"  live    : ERROR - {e}")
        print()

asyncio.run(main())
