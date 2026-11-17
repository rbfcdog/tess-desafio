begin;

-- Only used when reservation acknowledgement failed before the provider was called.
create function public.finish_unattempted_run(
  p_user_id uuid,p_run_id uuid,p_status text,p_content text,p_error_code text
) returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_run public.runs; v_step record; v_empty_usage bigint;
begin
  if p_status is null or p_status not in ('failed','interrupted') then raise exception 'INVALID_SETTLEMENT'; end if;
  select * into v_run from public.runs where id=p_run_id and user_id=p_user_id for update;
  if not found then raise exception 'RUN_NOT_FOUND'; end if;
  if v_run.status <> 'running' then return jsonb_build_object('ok',true); end if;
  -- The run lock also excludes a delayed reserve_model_step until settlement commits.
  for v_step in select id from public.model_steps where run_id=p_run_id and status='running'
  loop
    perform public.record_model_step(p_user_id,v_step.id,'failed',0,0);
  end loop;
  -- No committed steps means no provider call; otherwise finish_run derives persisted usage.
  if not exists (select 1 from public.model_steps where run_id=p_run_id) then v_empty_usage := 0; end if;
  return public.finish_run(p_user_id,p_run_id,p_status,p_content,v_empty_usage,v_empty_usage,p_error_code);
end;
$$;

-- One MVCC snapshot, not offset pages over a mutable updated_at ordering.
create function public.list_conversations(p_user_id uuid) returns jsonb
language sql stable security definer set search_path = '' as $$
  select jsonb_build_object('conversations',coalesce(jsonb_agg(
    jsonb_build_object('id',id,'title',title,'created_at',created_at,'updated_at',updated_at)
    order by updated_at desc,id desc
  ),'[]'::jsonb))
  from public.conversations where user_id=p_user_id;
$$;

revoke all on function public.finish_unattempted_run(uuid,uuid,text,text,text),public.list_conversations(uuid)
  from public,anon,authenticated;
grant execute on function public.finish_unattempted_run(uuid,uuid,text,text,text),public.list_conversations(uuid)
  to service_role;
notify pgrst,'reload schema';
commit;
