begin;

create or replace function public.record_model_step(
  p_user_id uuid,p_step_id uuid,p_status text,p_input_tokens bigint,p_output_tokens bigint
) returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_step public.model_steps; v_run public.runs; v_delta bigint;
begin
  if p_status is null or p_status not in ('completed','failed') or p_input_tokens < 0 or p_output_tokens < 0
     or (p_input_tokens is null) <> (p_output_tokens is null)
     or (p_status='completed' and p_input_tokens is null) then raise exception 'INVALID_STEP'; end if;
  select * into v_step from public.model_steps where id=p_step_id and user_id=p_user_id;
  if not found then raise exception 'STEP_NOT_FOUND'; end if;
  -- Always lock the run before its steps, including cancellation/recovery paths.
  select * into v_run from public.runs where id=v_step.run_id and user_id=p_user_id for update;
  if v_run.status <> 'running' then return jsonb_build_object('ok',true); end if;
  update public.model_steps set status=p_status,input_tokens=p_input_tokens,output_tokens=p_output_tokens,finished_at=now()
    where id=p_step_id and status='running';
  if found then
    -- Unknown usage keeps its full hold; known usage replaces only this step's estimate.
    v_delta := coalesce(p_input_tokens+p_output_tokens-v_step.reserved_tokens,0);
    update public.usage_accounts set reserved_tokens=reserved_tokens+v_delta where id='global';
    update public.runs set reserved_tokens=reserved_tokens+v_delta,updated_at=now() where id=v_run.id;
    perform public.record_audit(p_user_id,'model.step.'||p_status,
      jsonb_build_object('step_id',p_step_id,'kind',v_step.kind,'input_tokens',p_input_tokens,'output_tokens',p_output_tokens),v_run.conversation_id,v_run.id);
  end if;
  return jsonb_build_object('ok',true);
end;
$$;

notify pgrst,'reload schema';
commit;
