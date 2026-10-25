"""Strict context admission for the pinned SGLang 0.4.6.post5 tokenizer."""
from contextvars import ContextVar
from pdserve.core import ServeError
from pdserve.protocol import completion_options
from pdserve.service import Service

PATCH_ID='pdserve-context-strict-v1'


class GuardedService(Service):
    def __init__(self,backend,encoder,*args,**kwargs):
        self._output_limit=ContextVar('pdserve_output_limit_'+str(id(self)))
        async def bounded_encoder(prompt):
            ids=await encoder(prompt)
            if len(ids)+self._output_limit.get()>=self.config.context:
                raise ServeError(413,'Input plus maximum output must be strictly less than the pinned engine context limit')
            return ids
        super().__init__(backend,bounded_encoder,*args,**kwargs)

    async def submit(self,body,request_id):
        options=completion_options(body,self.model,request_id)
        token=self._output_limit.set(options['output_limit'])
        try:return await super().submit(body,request_id)
        finally:self._output_limit.reset(token)

    def state(self):
        result=super().state()
        result['gateway_admission_patch']=PATCH_ID
        result['context_limit_semantics']='input_plus_max_output_strictly_less'
        result['gateway_instance_id']=self.boot
        result['gateway_event_log']=str(self.telemetry.handle.name) if self.telemetry.handle else None
        return result
