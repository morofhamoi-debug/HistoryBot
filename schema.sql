-- История: схема Supabase PostgreSQL
create extension if not exists "pgcrypto";

create table if not exists users (
    id              bigint primary key,
    first_name      text,
    last_name       text,
    username        text,
    status          text not null default 'pending'
                    check (status in ('pending','approved','denied','blocked')),
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);

create table if not exists access_requests (
    id              bigserial primary key,
    user_id         bigint not null references users(id) on delete cascade,
    status          text not null default 'pending'
                    check (status in ('pending','approved','denied','blocked')),
    created_at      timestamptz not null default now(),
    resolved_at     timestamptz,
    resolved_by     bigint
);
create index if not exists idx_access_requests_user_status on access_requests(user_id, status);

create table if not exists business_connections (
    id              text primary key,
    owner_id        bigint not null references users(id) on delete cascade,
    is_enabled      boolean not null default true,
    connected_at    timestamptz not null default now(),
    updated_at      timestamptz not null default now()
);
create index if not exists idx_business_connections_owner on business_connections(owner_id);

create table if not exists chats (
    id              bigserial primary key,
    connection_id   text not null references business_connections(id) on delete cascade,
    chat_id         bigint not null,
    chat_type       text,
    title           text,
    username        text,
    first_name      text,
    last_name       text,
    owner_id        bigint not null references users(id) on delete cascade,
    created_at      timestamptz not null default now(),
    unique (connection_id, chat_id)
);
create index if not exists idx_chats_owner on chats(owner_id);
create index if not exists idx_chats_chat_id on chats(chat_id);

create table if not exists contacts (
    id              bigserial primary key,
    owner_id        bigint not null references users(id) on delete cascade,
    telegram_user_id bigint,
    first_name      text,
    last_name       text,
    username        text,
    avatar_url      text,
    created_at      timestamptz not null default now(),
    unique (owner_id, telegram_user_id)
);
create index if not exists idx_contacts_owner on contacts(owner_id);
create index if not exists idx_contacts_username on contacts(owner_id, username);

create table if not exists messages (
    id              bigserial primary key,
    connection_id   text not null references business_connections(id) on delete cascade,
    chat_id         bigint not null,
    message_id      bigint not null,
    owner_id        bigint not null references users(id) on delete cascade,
    sender_id       bigint,
    sender_name     text,
    sender_username text,
    text            text,
    media_type      text,
    media_file_id   text,
    sent_at         timestamptz,
    created_at      timestamptz not null default now(),
    unique (connection_id, chat_id, message_id)
);
create index if not exists idx_messages_owner_chat on messages(owner_id, chat_id);
create index if not exists idx_messages_sender on messages(owner_id, sender_id);

create table if not exists message_versions (
    id              bigserial primary key,
    message_id      bigint not null references messages(id) on delete cascade,
    old_text        text,
    new_text        text,
    edited_at       timestamptz not null default now()
);
create index if not exists idx_message_versions_message on message_versions(message_id);

create table if not exists deletion_events (
    id              bigserial primary key,
    connection_id   text not null references business_connections(id) on delete cascade,
    chat_id         bigint not null,
    message_ids     bigint[] not null,
    owner_id        bigint not null references users(id) on delete cascade,
    detected_at     timestamptz not null default now(),
    event_hash      text not null unique
);
create index if not exists idx_deletion_events_owner_chat on deletion_events(owner_id, chat_id);

create table if not exists media_files (
    id              bigserial primary key,
    message_id      bigint not null references messages(id) on delete cascade,
    owner_id        bigint not null references users(id) on delete cascade,
    media_type      text,
    storage_path    text not null,
    file_size       bigint,
    created_at      timestamptz not null default now(),
    unique (message_id)
);
create index if not exists idx_media_files_owner on media_files(owner_id);

create table if not exists notifications (
    id              bigserial primary key,
    owner_id        bigint not null references users(id) on delete cascade,
    type            text not null,
    payload         jsonb not null default '{}'::jsonb,
    created_at      timestamptz not null default now(),
    read_at         timestamptz
);
create index if not exists idx_notifications_owner on notifications(owner_id, read_at);

create table if not exists audit_logs (
    id              bigserial primary key,
    user_id         bigint,
    action          text not null,
    details         jsonb not null default '{}'::jsonb,
    created_at      timestamptz not null default now()
);
create index if not exists idx_audit_logs_created on audit_logs(created_at desc);
