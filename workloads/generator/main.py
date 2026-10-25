import argparse
import json
import os
import random
import time
from pathlib import Path


def seed_data(args):
    import psycopg

    with psycopg.connect(args.dsn) as connection:
        connection.execute("DELETE FROM items")
        connection.execute(
            """
            INSERT INTO items (id, name, score, active)
            SELECT n,
                   'item-' || n::text,
                   ((n * 7919 + %s) %% 10000)::integer,
                   (n + %s) %% 5 <> 0
            FROM generate_series(1::bigint, %s::bigint) AS t(n)
            """,
            (args.seed, args.seed, args.rows),
        )


def generate_plan(args):
    rng = random.Random(args.seed)
    alive = list(range(1, args.initial_rows + 1))
    next_id = args.initial_rows + 1
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8") as output:
        for step in range(args.count):
            op = rng.choices(("insert", "update", "delete"), weights=(2, 6, 2))[0]
            if not alive:
                op = "insert"

            event = {"at_ms": step * args.interval_ms, "op": op}
            if op == "insert":
                event.update(
                    id=next_id,
                    name=f"item-{next_id}",
                    score=rng.randrange(10000),
                    active=rng.random() < 0.8,
                )
                alive.append(next_id)
                next_id += 1
            else:
                index = rng.randrange(len(alive))
                event["id"] = alive[index]
                if op == "delete":
                    alive[index] = alive[-1]
                    alive.pop()
                else:
                    field = rng.choice(("score", "active", "name"))
                    event["field"] = field
                    if field == "score":
                        event["value"] = rng.randrange(10000)
                    elif field == "name":
                        event["value"] = f"item-{event['id']}-v{step}"

            output.write(json.dumps(event, ensure_ascii=False) + "\n")


def execute_event(connection, event):
    op = event["op"]
    if op == "insert":
        cursor = connection.execute(
            "INSERT INTO items (id, name, score, active) VALUES (%s, %s, %s, %s)",
            (event["id"], event["name"], event["score"], event["active"]),
        )
    elif op == "delete":
        cursor = connection.execute("DELETE FROM items WHERE id = %s", (event["id"],))
    elif op == "update":
        field = event["field"]
        if field == "active":
            cursor = connection.execute("UPDATE items SET active = NOT active WHERE id = %s", (event["id"],))
        elif field == "score":
            cursor = connection.execute("UPDATE items SET score = %s WHERE id = %s", (event["value"], event["id"]))
        elif field == "name":
            cursor = connection.execute("UPDATE items SET name = %s WHERE id = %s", (event["value"], event["id"]))
        else:
            raise ValueError(f"Unknown field: {field}")
    else:
        raise ValueError(f"Unknown operation: {op}")

    if cursor.rowcount != 1:
        raise RuntimeError(f"Unexpected affected row count for {event}: {cursor.rowcount}")


def run_plan(args):
    import psycopg

    with psycopg.connect(args.dsn, autocommit=True) as connection:
        with Path(args.input).open(encoding="utf-8") as source:
            if args.log:
                Path(args.log).parent.mkdir(parents=True, exist_ok=True)
            log = Path(args.log).open("w", encoding="utf-8") if args.log else None
            try:
                origin = time.monotonic_ns()
                for index, line in enumerate(source):
                    event = json.loads(line)
                    due = origin + event["at_ms"] * 1_000_000
                    remaining = (due - time.monotonic_ns()) / 1_000_000_000
                    if remaining > 0:
                        time.sleep(remaining)

                    started = time.time_ns()
                    execute_event(connection, event)
                    finished = time.time_ns()
                    if log:
                        log.write(json.dumps({
                            "index": index,
                            "op": event["op"],
                            "id": event["id"],
                            "scheduled_ms": event["at_ms"],
                            "started_ns": started,
                            "commit_ack_ns": finished,
                        }) + "\n")
                        log.flush()
            finally:
                if log:
                    log.close()


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    seed = commands.add_parser("seed")
    seed.add_argument("--dsn", default=os.getenv("DATABASE_URL"), required=os.getenv("DATABASE_URL") is None)
    seed.add_argument("--rows", type=int, default=1000)
    seed.add_argument("--seed", type=int, default=42)
    seed.set_defaults(action=seed_data)

    plan = commands.add_parser("plan")
    plan.add_argument("--initial-rows", type=int, default=1000)
    plan.add_argument("--count", type=int, default=100)
    plan.add_argument("--interval-ms", type=int, default=100)
    plan.add_argument("--seed", type=int, default=42)
    plan.add_argument("--output", default="workload.jsonl")
    plan.set_defaults(action=generate_plan)

    run = commands.add_parser("run")
    run.add_argument("--dsn", default=os.getenv("DATABASE_URL"), required=os.getenv("DATABASE_URL") is None)
    run.add_argument("--input", default="workload.jsonl")
    run.add_argument("--log")
    run.set_defaults(action=run_plan)

    args = parser.parse_args()
    if args.command == "seed" and args.rows < 0:
        parser.error("--rows must be non-negative")
    if args.command == "plan" and (args.initial_rows < 0 or args.count < 0 or args.interval_ms < 0):
        parser.error("--initial-rows, --count and --interval-ms must be non-negative")
    args.action(args)


if __name__ == "__main__":
    main()
