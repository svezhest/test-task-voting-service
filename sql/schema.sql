create table polls (
    id             uuid primary key,
    question       text not null,
    type           text not null,
    status         text not null default 'draft',
    window_start   timestamptz not null,
    window_end     timestamptz not null,
    grace_s        integer not null,
    salt           bytea,
    config_version bigint not null default 0,
    created_at     timestamptz not null default now()
);

create table options (
    poll_id uuid not null references polls,
    idx     smallint not null,
    label   text not null,
    primary key (poll_id, idx)
);

-- option_idx = -1: number of votes (a multi vote adds +1 to several options, so options do not sum to it)
create table results (
    poll_id     uuid not null,
    partition   integer not null,
    option_idx  smallint not null,
    counted     bigint not null,
    total       bigint not null,
    last_offset bigint not null,
    primary key (poll_id, partition, option_idx)
);

create table stage_progress (
    poll_id    uuid not null,
    stage      smallint not null,
    partition  integer not null,
    votes      bigint not null,
    end_offset bigint not null,
    done_at    timestamptz,
    primary key (poll_id, stage, partition)
);

create table fp_counts (
    poll_id   uuid not null,
    partition integer not null,
    blob      bytea not null,
    primary key (poll_id, partition)
);

create table timeline (
    poll_id   uuid not null,
    partition integer not null,
    second    integer not null,
    votes     bigint not null,
    primary key (poll_id, partition, second)
);

-- key_limit, ip_ceiling: votes rejected by each; ip_hist: {"1": {"ips": .., "votes": ..}, "2-10": ..., ...}
create table stage2_stats (
    poll_id     uuid not null,
    partition   integer not null,
    key_limit   bigint not null,
    ip_ceiling  bigint not null,
    ip_hist     jsonb not null,
    last_offset bigint not null,
    primary key (poll_id, partition)
);
