begin;

-- ponytail: 5 MB/file and 50 MB/account keep bytea practical; move blobs to private object storage above this ceiling.
create table public.attachments (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.profiles(id) on delete cascade,
  conversation_id uuid not null,
  filename text not null check (octet_length(filename) between 1 and 240 and filename !~ '[[:cntrl:]/\\]'),
  media_type text not null check (media_type in ('image/png','image/jpeg','image/webp','application/pdf')),
  size_bytes integer not null check (size_bytes between 1 and 5242880),
  page_count integer,
  created_at timestamptz not null default clock_timestamp(),
  unique (id,user_id,conversation_id),
  foreign key (conversation_id,user_id) references public.conversations(id,user_id) on delete cascade,
  check ((media_type='application/pdf' and page_count is not null and page_count between 1 and 20) or (media_type<>'application/pdf' and page_count is null))
);
create index attachments_owner_conversation on public.attachments(user_id,conversation_id,created_at,id);
create table public.attachment_blobs (
  attachment_id uuid primary key references public.attachments(id) on delete cascade,
  data bytea not null check (octet_length(data) between 1 and 5242880)
);
alter table public.messages add constraint messages_identity_owner unique (id,user_id,conversation_id);
create table public.message_attachments (
  message_id uuid not null,
  attachment_id uuid not null,
  user_id uuid not null,
  conversation_id uuid not null,
  primary key (message_id,attachment_id),
  foreign key (message_id,user_id,conversation_id) references public.messages(id,user_id,conversation_id) on delete cascade,
  foreign key (attachment_id,user_id,conversation_id) references public.attachments(id,user_id,conversation_id) on delete cascade
);
create index message_attachments_file on public.message_attachments(attachment_id);
create table public.conversation_shares (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null,
  conversation_id uuid not null,
  token_hash text not null unique check (token_hash ~ '^[a-f0-9]{64}$'),
  snapshot jsonb not null,
  created_at timestamptz not null default clock_timestamp(),
  expires_at timestamptz not null,
  revoked_at timestamptz,
  foreign key (conversation_id,user_id) references public.conversations(id,user_id) on delete cascade,
  check (expires_at>created_at and expires_at<=created_at+interval '7 days')
);
create index conversation_shares_owner on public.conversation_shares(user_id,conversation_id,created_at);
create table public.share_attachments (
  share_id uuid not null references public.conversation_shares(id) on delete cascade,
  attachment_id uuid not null references public.attachments(id) on delete cascade,
  primary key (share_id,attachment_id)
);

create function public.store_attachment(
  p_user_id uuid,p_conversation_id uuid,p_filename text,p_media_type text,p_data_base64 text,p_page_count integer default null
) returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_data bytea; v_attachment public.attachments;
begin
  -- Every insertion/removal/bind for an account takes this same lock: concurrent uploads cannot overspend quota.
  perform 1 from public.profiles where id=p_user_id for update;
  if not found then return jsonb_build_object('error','CONVERSATION_NOT_FOUND'); end if;
  perform 1 from public.conversations where id=p_conversation_id and user_id=p_user_id for update;
  if not found then return jsonb_build_object('error','CONVERSATION_NOT_FOUND'); end if;
  if p_data_base64 is null or length(p_data_base64)>6990508
    or p_filename is null or octet_length(p_filename) not between 1 and 240
    or p_filename ~ '[[:cntrl:]/\\]' or p_filename<>btrim(p_filename) or left(p_filename,1)='.'
    or p_media_type is null or p_media_type not in ('image/png','image/jpeg','image/webp','application/pdf')
    or (p_media_type='application/pdf' and (p_page_count is null or p_page_count not between 1 and 20))
    or (p_media_type<>'application/pdf' and p_page_count is not null)
  then return jsonb_build_object('error','INVALID_ATTACHMENT'); end if;
  begin
    v_data := decode(p_data_base64,'base64');
  exception when invalid_parameter_value or data_exception then
    return jsonb_build_object('error','INVALID_ATTACHMENT');
  end;
  if octet_length(v_data) not between 1 and 5242880 then return jsonb_build_object('error','INVALID_ATTACHMENT'); end if;
  if (select coalesce(sum(size_bytes),0) from public.attachments where user_id=p_user_id)+octet_length(v_data)>52428800
    then return jsonb_build_object('error','ATTACHMENT_QUOTA_EXCEEDED'); end if;
  insert into public.attachments(user_id,conversation_id,filename,media_type,size_bytes,page_count)
    values (p_user_id,p_conversation_id,p_filename,p_media_type,octet_length(v_data),p_page_count) returning * into v_attachment;
  insert into public.attachment_blobs(attachment_id,data) values (v_attachment.id,v_data);
  perform public.record_audit(p_user_id,'attachment.created',jsonb_build_object('attachment_id',v_attachment.id,'size_bytes',v_attachment.size_bytes),p_conversation_id);
  return jsonb_build_object('attachment',to_jsonb(v_attachment)-'user_id');
end;
$$;

create function public.get_attachment(p_user_id uuid,p_attachment_id uuid,p_conversation_id uuid default null)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_result jsonb;
begin
  select jsonb_build_object('attachment',to_jsonb(a)-'user_id','data_base64',replace(encode(b.data,'base64'),E'\n','')) into v_result
    from public.attachments a join public.attachment_blobs b on b.attachment_id=a.id
    where a.id=p_attachment_id and a.user_id=p_user_id and (p_conversation_id is null or a.conversation_id=p_conversation_id);
  return coalesce(v_result,jsonb_build_object('error','ATTACHMENT_NOT_FOUND'));
end;
$$;

create function public.remove_attachment(p_user_id uuid,p_attachment_id uuid)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_attachment public.attachments;
begin
  perform 1 from public.profiles where id=p_user_id for update;
  select * into v_attachment from public.attachments where id=p_attachment_id and user_id=p_user_id for update;
  if not found then return jsonb_build_object('error','ATTACHMENT_NOT_FOUND'); end if;
  if exists (select 1 from public.message_attachments where attachment_id=p_attachment_id)
    or exists (select 1 from public.share_attachments where attachment_id=p_attachment_id)
    then return jsonb_build_object('error','ATTACHMENT_BOUND'); end if;
  delete from public.attachments where id=p_attachment_id;
  perform public.record_audit(p_user_id,'attachment.removed',jsonb_build_object('attachment_id',p_attachment_id),v_attachment.conversation_id);
  return jsonb_build_object('ok',true);
end;
$$;

create function public.bind_run_attachments(p_user_id uuid,p_run_id uuid,p_attachment_ids uuid[])
returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_run public.runs; v_message_id uuid; v_existing uuid[]; v_ids uuid[];
begin
  perform 1 from public.profiles where id=p_user_id for update;
  select * into v_run from public.runs where id=p_run_id and user_id=p_user_id;
  if not found then raise exception 'RUN_NOT_FOUND'; end if;
  if p_attachment_ids is null or cardinality(p_attachment_ids)>5
    or exists (select 1 from unnest(p_attachment_ids) a where a is null)
    or cardinality(p_attachment_ids)<>(select count(distinct a) from unnest(p_attachment_ids) a)
    then raise exception 'INVALID_ATTACHMENT'; end if;
  select id into v_message_id from public.messages where run_id=p_run_id and user_id=p_user_id and role='user';
  if not found then raise exception 'RUN_NOT_FOUND'; end if;
  perform 1 from public.attachments where id=any(p_attachment_ids) and user_id=p_user_id and conversation_id=v_run.conversation_id order by id for update;
  if (select count(*) from public.attachments where id=any(p_attachment_ids) and user_id=p_user_id and conversation_id=v_run.conversation_id)<>cardinality(p_attachment_ids)
    then raise exception 'ATTACHMENT_NOT_FOUND'; end if;
  select coalesce(array_agg(attachment_id order by attachment_id),'{}'::uuid[]) into v_existing from public.message_attachments where message_id=v_message_id;
  select coalesce(array_agg(a order by a),'{}'::uuid[]) into v_ids from unnest(p_attachment_ids) a;
  if cardinality(v_existing)>0 then
    if v_existing<>v_ids then raise exception 'INVALID_ATTACHMENT'; end if;
    return jsonb_build_object('ok',true);
  end if;
  if v_run.status<>'running' then raise exception 'INVALID_ATTACHMENT'; end if;
  insert into public.message_attachments(message_id,attachment_id,user_id,conversation_id)
    select v_message_id,a,p_user_id,v_run.conversation_id from unnest(p_attachment_ids) a;
  return jsonb_build_object('ok',true);
end;
$$;

create function public.create_share(p_user_id uuid,p_conversation_id uuid,p_token_hash text,p_expires_in_days integer default 7)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_snapshot jsonb; v_share public.conversation_shares; v_now timestamptz := clock_timestamp();
begin
  perform 1 from public.profiles where id=p_user_id for update;
  perform 1 from public.conversations where id=p_conversation_id and user_id=p_user_id for update;
  if not found then return jsonb_build_object('error','CONVERSATION_NOT_FOUND'); end if;
  if p_expires_in_days is null or p_expires_in_days not between 1 and 7 or p_token_hash is null or p_token_hash !~ '^[a-f0-9]{64}$'
    then return jsonb_build_object('error','INVALID_ATTACHMENT'); end if;
  -- A single SQL statement supplies one MVCC snapshot for title, messages, attachments and tool executions.
  -- Whitelists deliberately exclude system settings, summaries/model context, configurations and credentials.
  select jsonb_build_object('title',c.title,'created_at',v_now,
    'messages',coalesce((select jsonb_agg(jsonb_build_object(
      'id',m.id,'role',m.role,'content',m.content,'status',m.status,'run_id',m.run_id,'created_at',m.created_at,
      'attachments',coalesce((select jsonb_agg(to_jsonb(a)-'user_id' order by a.created_at,a.id)
        from public.message_attachments ma join public.attachments a on a.id=ma.attachment_id
        where ma.message_id=m.id and ma.user_id=p_user_id and ma.conversation_id=p_conversation_id),'[]'::jsonb)
      ) order by m.created_at,m.id) from public.messages m where m.user_id=p_user_id and m.conversation_id=p_conversation_id),'[]'::jsonb),
    'tool_calls',coalesce((select jsonb_agg(jsonb_build_object(
      'id',t.id,'run_id',t.run_id,'name',t.name,'status',t.status,'arguments',t.arguments,'result',t.result,
      'error',t.error,'created_at',t.created_at,'finished_at',t.finished_at) order by t.created_at,t.id)
      from public.tool_executions t where t.user_id=p_user_id and t.conversation_id=p_conversation_id),'[]'::jsonb))
    into v_snapshot from public.conversations c where c.id=p_conversation_id and c.user_id=p_user_id;
  insert into public.conversation_shares(user_id,conversation_id,token_hash,snapshot,created_at,expires_at)
    values (p_user_id,p_conversation_id,p_token_hash,v_snapshot,v_now,v_now+make_interval(days=>p_expires_in_days)) returning * into v_share;
  insert into public.share_attachments(share_id,attachment_id)
    select distinct v_share.id,(a.value->>'id')::uuid
    from jsonb_array_elements(v_snapshot->'messages') m cross join lateral jsonb_array_elements(m.value->'attachments') a;
  perform public.record_audit(p_user_id,'share.created',jsonb_build_object('share_id',v_share.id,'expires_at',v_share.expires_at),p_conversation_id);
  return jsonb_build_object('share',jsonb_build_object('id',v_share.id,'created_at',v_share.created_at,'expires_at',v_share.expires_at,'revoked_at',v_share.revoked_at));
end;
$$;

create function public.list_shares(p_user_id uuid,p_conversation_id uuid)
returns jsonb language plpgsql security definer set search_path = '' as $$
begin
  if not exists (select 1 from public.conversations where id=p_conversation_id and user_id=p_user_id)
    then return jsonb_build_object('error','CONVERSATION_NOT_FOUND'); end if;
  return jsonb_build_object('shares',coalesce((select jsonb_agg(jsonb_build_object(
    'id',id,'created_at',created_at,'expires_at',expires_at,'revoked_at',revoked_at) order by created_at desc,id)
    from public.conversation_shares where user_id=p_user_id and conversation_id=p_conversation_id),'[]'::jsonb));
end;
$$;

create function public.revoke_share(p_user_id uuid,p_share_id uuid)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_share public.conversation_shares;
begin
  update public.conversation_shares set revoked_at=coalesce(revoked_at,clock_timestamp()) where id=p_share_id and user_id=p_user_id returning * into v_share;
  if not found then return jsonb_build_object('error','SHARE_NOT_FOUND'); end if;
  perform public.record_audit(p_user_id,'share.revoked',jsonb_build_object('share_id',p_share_id),v_share.conversation_id);
  return jsonb_build_object('ok',true);
end;
$$;

create function public.read_share(p_token_hash text)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_result jsonb;
begin
  select snapshot||jsonb_build_object('expires_at',expires_at) into v_result from public.conversation_shares
    where token_hash=p_token_hash and revoked_at is null and expires_at>clock_timestamp();
  return coalesce(v_result,jsonb_build_object('error','SHARE_NOT_FOUND'));
end;
$$;

create function public.read_shared_attachment(p_token_hash text,p_attachment_id uuid)
returns jsonb language plpgsql security definer set search_path = '' as $$
declare v_result jsonb;
begin
  -- Match the frozen membership as well as the live token on every file request, not just the page request.
  select jsonb_build_object('attachment',to_jsonb(a)-'user_id','data_base64',replace(encode(b.data,'base64'),E'\n','')) into v_result
    from public.conversation_shares s join public.share_attachments sa on sa.share_id=s.id
    join public.attachments a on a.id=sa.attachment_id join public.attachment_blobs b on b.attachment_id=a.id
    where s.token_hash=p_token_hash and s.revoked_at is null and s.expires_at>clock_timestamp() and sa.attachment_id=p_attachment_id;
  return coalesce(v_result,jsonb_build_object('error','SHARE_NOT_FOUND'));
end;
$$;

alter table public.attachments enable row level security;
alter table public.attachment_blobs enable row level security;
alter table public.message_attachments enable row level security;
alter table public.conversation_shares enable row level security;
alter table public.share_attachments enable row level security;
create policy own_attachments on public.attachments for select to authenticated using (user_id=(select auth.uid()));
create policy own_message_attachments on public.message_attachments for select to authenticated using (user_id=(select auth.uid()));
revoke all on public.attachments,public.attachment_blobs,public.message_attachments,public.conversation_shares,public.share_attachments from anon,authenticated,service_role;
grant select on public.attachments,public.message_attachments to authenticated,service_role;
revoke all on function public.store_attachment(uuid,uuid,text,text,text,integer),public.get_attachment(uuid,uuid,uuid),
  public.remove_attachment(uuid,uuid),public.bind_run_attachments(uuid,uuid,uuid[]),public.create_share(uuid,uuid,text,integer),
  public.list_shares(uuid,uuid),public.revoke_share(uuid,uuid),public.read_share(text),public.read_shared_attachment(text,uuid)
  from public,anon,authenticated;
grant execute on function public.store_attachment(uuid,uuid,text,text,text,integer),public.get_attachment(uuid,uuid,uuid),
  public.remove_attachment(uuid,uuid),public.bind_run_attachments(uuid,uuid,uuid[]),public.create_share(uuid,uuid,text,integer),
  public.list_shares(uuid,uuid),public.revoke_share(uuid,uuid),public.read_share(text),public.read_shared_attachment(text,uuid)
  to service_role;

commit;
