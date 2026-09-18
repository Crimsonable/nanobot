"""Fixed host command used by the bash hook. Original shell text never reaches this process."""
import json
import os
import sys
import urllib.request


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("expected one execution ticket")
    request = urllib.request.Request(os.environ["BUCKET_RPC_URL"] + "/execute",
        data=json.dumps({"ticket": sys.argv[1]}).encode(),
        headers={"Authorization": "Bearer " + os.environ["BUCKET_RPC_TOKEN"],
                 "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=650) as response:
        result = json.load(response)
    print(result["output"], end="")
    raise SystemExit(result["exit_code"])


if __name__ == "__main__":
    main()
