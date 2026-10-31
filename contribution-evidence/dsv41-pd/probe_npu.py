import json

import torch
import torch_npu


def main():
    torch.npu.set_device(0)
    torch.npu.config.allow_internal_format = True
    print(json.dumps({"device": torch.npu.get_device_name(0), "torch_npu": torch_npu.__version__}), flush=True)
    # Matches the failing INT8 ND weight shape, without loading the model.
    x = (torch.arange(1280 * 16384, dtype=torch.int32) % 251 - 125).to(torch.int8).reshape(1280, 16384)
    y = torch_npu.npu_format_cast(x.npu(), 29)
    torch.npu.synchronize()
    assert torch_npu.get_npu_format(y) == 29
    z = torch_npu.npu_format_cast(y, 2).cpu()
    assert torch.equal(x, z)
    a = torch.ones((64, 64), dtype=torch.float16, device="npu")
    assert torch.equal((a @ a).cpu(), torch.full((64, 64), 64, dtype=torch.float16))
    torch.npu.synchronize()
    print("REAL_NPU_FORMAT_AND_MATMUL_PASS", flush=True)


if __name__ == "__main__":
    main()
