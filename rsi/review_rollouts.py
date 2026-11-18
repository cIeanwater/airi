"""Capture one local E2B proposal per supervised decision for a human or GPT reviewer."""
import argparse
from pathlib import Path

from .common import digest, read_json, read_jsonl, write_json, file_hash
from .dataset import _native
from .finetune import configuration, load_base_model, _base_identity
from .pilot_eval import generation_function, split_completion


def capture(args):
    from peft import PeftModel
    from transformers import AutoTokenizer, set_seed
    import torch
    cfg=configuration(read_json(args.config))
    lineage=read_json(args.adapter/"finetune-lineage.json")
    if cfg["model"] and lineage["base"] != _base_identity(cfg["model"]):
        raise ValueError("Rollout adapter and base model differ")
    set_seed(cfg["seed"])
    tok=AutoTokenizer.from_pretrained(cfg["tokenizer"],local_files_only=True)
    model=PeftModel.from_pretrained(load_base_model(cfg),args.adapter,local_files_only=True).eval()
    generate=generation_function(model,tok,cfg,args.budget)
    args.output.mkdir(parents=True,exist_ok=True)
    identity={"weights":file_hash(args.adapter/"adapter_model.safetensors"),"budget":args.budget,
              "batch_size":args.batch_size,"config":digest(cfg),"gpu":torch.cuda.get_device_name(0)}
    work=[]
    for row in read_jsonl(args.rows):
        for i,target in enumerate(row["messages"]):
            if not target.get("loss_weight"): continue
            key=digest([row["id"],i]); p=args.output/(key+".json")
            bind={"id":row["id"],"index":i,"candidate":digest(row),**identity}
            if p.exists():
                if read_json(p)["binding"]!=bind: raise ValueError("Stale rollout at "+str(p))
                continue
            work.append((p,bind,(_native(row["messages"][:i],tok),row["tools"])))
    for start in range(0,len(work),args.batch_size):
        batch=work[start:start+args.batch_size]
        for (path,bind,_),(raw,n,prefill) in zip(batch,generate([t[-1] for t in batch]),strict=True):
            try:
                _,calls,text=split_completion(raw,starts_in_thought=prefill);error=None
            except ValueError as exc:
                calls,text,error=[],None,str(exc)
            write_json(path,{"binding":bind,"raw":raw,"tokens":n,"calls":calls,"text":text,"error":error})
        print("saved",min(start+args.batch_size,len(work)),"/",len(work),flush=True)


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    for name in ("config","adapter","rows","output"):
        p.add_argument("--"+name,required=True,type=Path)
    p.add_argument("--budget",type=int,default=192)
    p.add_argument("--batch-size",type=int,default=2)
    capture(p.parse_args())
