// EthrexDB Data:
// 1. CF_CANONICAL_BLOCK_HASHES: [u8; 8] => [u8; 32]
// 2. CF_BLOCK_NUMBERS:          [u8; 32] => [u8; 8]
// 3. CF_HEADERS:                [u8; 32] => BlockHeaderRLP
// 4. CF_BODIES:                 [u8; 32] => BlockBodyRLP
// 5. CF_ACCOUNT_CODES:          [u8; 32] => AccountCodeRLP
// 6. CF_STATE_TRIE_NODES:       [u8; 32] => Vec<u8>
// 7. CF_STORAGE_TRIE_NODES:     [u8; 64] => Vec<u8>
//
// GethDB Data:
// Hashes: either ancient/chain/hashes.{meta,cidx,cdat} or by querying
// ['h' || block_num || 'n'] in the statedb.
// Headers: either ancient/chain/headers.{meta,cidx,cdat} or
// 'h'-prefixed values in statedb. Note the full key there is
// ['h' || block_num || header_hash], where the header_hash can be
// determined as stated above.
// Bodies: either ancient/chain/bodies.{meta,cidx,cdat} or 'b'-prefixed
// values in statedb. We shall use the same mechanism as for headers,
// actually reusing the computed hash.
// Trie Nodes: only in statedb, with only the hash as key.
// Bytecodes: in statedb, ['c' || code_hash ].
use clap::Parser;
use ethrex_common::H256;
use ethrex_common::types::Code;
use ethrex_common::types::{Block, BlockBody, BlockHeader, BlockNumber};
use ethrex_common::utils::keccak;
use ethrex_common::{
    constants::{EMPTY_KECCAK_HASH, EMPTY_TRIE_HASH},
    types::AccountState,
};
use ethrex_config::networks::Network;
use ethrex_rlp::decode::RLPDecode;
use ethrex_rlp::decode::decode_bytes;
use ethrex_rlp::decode::decode_rlp_item;
use ethrex_storage::EngineType;
use ethrex_storage::Store;
use ethrex_trie::Nibbles;
use ethrex_trie::Node;
use ethrex_trie::TrieNode;
use eyre::OptionExt;
use rocksdb::IteratorMode;
use rocksdb::{DBWithThreadMode, Options, Range, SingleThreaded};
use std::collections::BTreeMap;
use std::collections::BTreeSet;
use std::collections::HashMap;
use std::fs::File;
use std::io;
use std::io::{ErrorKind, Read};
use std::os::unix::fs::{FileExt, MetadataExt};
use std::path::{Path, PathBuf};
use std::sync::LazyLock;
use std::time::{Duration, Instant};
use tracing::{debug, error, info};
use tracing_subscriber::{EnvFilter, FmtSubscriber};

const BLOCK_HASH_LOOKUP_DEPTH: u64 = 1024;
// Mirrors the FKV completion marker length used by ethrex_storage::Store
// (see store.rs `vec![0xff; 64]`). Keep in sync if ethrex changes it.
const FKV_COMPLETION_MARKER_LEN: usize = 64;
static GETH_DB_PATH: LazyLock<&str> = LazyLock::new(|| {
    AsRef::<Path>::as_ref(&Args::parse().input_dir)
        .join("chaindata")
        .to_string_lossy()
        .into_owned()
        .leak()
});
static GETH_JOURNAL_PATH: LazyLock<&str> = LazyLock::new(|| {
    AsRef::<Path>::as_ref(&Args::parse().input_dir)
        .join("triedb/merkle.journal")
        .to_string_lossy()
        .into_owned()
        .leak()
});

pub fn main() -> eyre::Result<()> {
    let args = Args::parse();
    let env_filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    tracing::subscriber::set_global_default(
        FmtSubscriber::builder()
            .with_env_filter(env_filter)
            .finish(),
    )
    .expect("setting default subscriber failed");
    let store = Store::new(&args.output_dir, EngineType::RocksDB)?;
    geth2ethrex(store, args.block_number, &args)
}

fn geth2ethrex(mut store: Store, block_number: BlockNumber, args: &Args) -> eyre::Result<()> {
    let log_interval = Duration::from_secs(args.log_interval);
    let rt = tokio::runtime::Builder::new_current_thread()
        .build()
        .unwrap();

    // ----- Stage 1: Genesis init -----------------------------------------
    // We must compute total stages before logging the first banner. To do
    // that we need to know whether a journal exists; we peek without
    // opening the geth DB. This is cheap and only requires a stat-call.
    let journal_present = {
        // The journal sits either at <input>/triedb/merkle.journal as a
        // file, or inside the state DB under key "TrieJournal". We can't
        // check the DB key without opening it, so we treat journal-present
        // as "file exists OR the DB might have it" — but we'll re-evaluate
        // once we've actually opened the DB. To keep the M/N count stable
        // we always reserve a slot for diff layers and skip its banner
        // gracefully if it ends up empty.
        Path::new(*GETH_JOURNAL_PATH).exists()
    };

    let total_stages = plan_stage_count(args, journal_present);
    let mut runner = StageRunner::new(total_stages, log_interval);

    {
        let s = runner.begin("Genesis init", "items", Some(1), None);
        rt.block_on(store.add_initial_state(args.network.get_genesis()?))?;
        s.finish_with_done(1);
    }

    // ----- Stage 2: Read geth metadata ----------------------------------
    let (gethdb, hashes, header, block_hash, numbers_and_hashes);
    {
        let s = runner.begin(
            "Read geth metadata",
            "items",
            Some(BLOCK_HASH_LOOKUP_DEPTH + 1),
            None,
        );
        info!("  Opening Geth DB at {}", *GETH_DB_PATH);
        gethdb = GethDB::open()?;
        hashes = gethdb.read_hashes_from_gethdb(
            block_number.saturating_sub(BLOCK_HASH_LOOKUP_DEPTH),
            block_number,
        )?;
        block_hash = *hashes.last().ok_or_eyre("missing block hash")?;
        let [header_rlp, _] = gethdb.read_block_from_gethdb(block_number, block_hash)?;
        header = BlockHeader::decode(&header_rlp)?;
        numbers_and_hashes = hashes
            .iter()
            .rev()
            .enumerate()
            .map(|(i, hash)| (block_number - i as u64, H256::from_slice(hash)))
            .collect::<Vec<_>>();
        s.finish_with_done(numbers_and_hashes.len() as u64);
    }

    let mut codes = BTreeSet::new();
    let mut total_accounts: u64 = 0;

    if !args.validate_only {
        // ----- Stage 3: Headers + bodies -------------------------------
        {
            let mut s = runner.begin(
                "Headers + bodies",
                "blocks",
                Some(numbers_and_hashes.len() as u64),
                None,
            );
            for (number, hash) in numbers_and_hashes.iter() {
                let Ok([header_rlp, body_rlp]) =
                    gethdb.read_block_from_gethdb(*number, hash.to_fixed_bytes())
                else {
                    debug!("Couldn't obtain block {number}");
                    s.tick(1, 0);
                    continue;
                };
                let header: BlockHeader = RLPDecode::decode(&header_rlp)?;
                let body: BlockBody = RLPDecode::decode(&body_rlp)?;
                let bytes = (header_rlp.len() + body_rlp.len()) as u64;
                rt.block_on(store.add_block(Block::new(header, body)))?;
                s.tick(1, bytes);
            }
            s.finish();
        }

        // ----- Stage 4: Canonical hashes -------------------------------
        {
            let s = runner.begin("Canonical hashes", "items", Some(1), None);
            rt.block_on(store.forkchoice_update(
                numbers_and_hashes.clone(),
                block_number,
                H256::from_slice(&block_hash),
                None,
                None,
            ))?;
            s.finish_with_done(1);
        }

        if !gethdb.is_path_based()? {
            eyre::bail!("hash-based not supported");
        }

        // ----- Stage 5: State trie --------------------------------------
        let db_root_hash = keccak(gethdb.state_db.get(b"A").unwrap().unwrap());
        let state_trie = store.open_direct_state_trie(*EMPTY_TRIE_HASH)?;
        {
            let est_keys = approx_key_count(&gethdb.state_db, b"A", b"B");
            let total = if est_keys > 0 { Some(est_keys) } else { None };
            let mut s = runner.begin("State trie", "nodes", total, None);
            let mut nodes_to_push: Vec<(Nibbles, Vec<u8>)> = Vec::new();
            for item in gethdb
                .state_db
                .iterator(IteratorMode::From(b"A", rocksdb::Direction::Forward))
                .take_while(|item| item.as_ref().map(|(k, _)| k.starts_with(b"A")).unwrap())
            {
                let (k, v) = item?;
                debug_assert!(k.len() <= 64);
                debug_assert!(!k.is_empty());
                debug_assert_eq!(k[0], b'A');
                let node = Node::decode(&v)?;
                let path = Nibbles::from_hex(k[1..].to_vec());
                let item_bytes = (k.len() + v.len()) as u64;
                nodes_to_push.push((path, v.into()));
                if nodes_to_push.len() > 100_000 {
                    state_trie
                        .db()
                        .put_batch(std::mem::take(&mut nodes_to_push))?;
                }
                if let Node::Leaf(leaf) = node {
                    let state = AccountState::decode(&leaf.value)?;
                    debug_assert_ne!(state.code_hash, H256::zero());
                    debug_assert_ne!(state.storage_root, H256::zero());
                    if state.code_hash != *EMPTY_KECCAK_HASH {
                        codes.insert(state.code_hash);
                    }
                    total_accounts += 1;
                }
                s.tick(1, item_bytes);
            }
            state_trie.db().put_batch(nodes_to_push)?;
            s.finish();
        }

        // ----- Stage 6: Storage tries -----------------------------------
        {
            let est_keys = approx_key_count(&gethdb.state_db, b"O", b"P");
            let total = if est_keys > 0 { Some(est_keys) } else { None };
            let mut s = runner.begin("Storage tries", "nodes", total, None);
            let mut storages_to_write: HashMap<H256, Vec<TrieNode>> = Default::default();
            for (flush_count, item) in gethdb
                .state_db
                .iterator(IteratorMode::From(b"O", rocksdb::Direction::Forward))
                .take_while(|item| item.as_ref().map(|(k, _)| k.starts_with(b"O")).unwrap())
                .enumerate()
            {
                let (k, v) = item?;
                let item_bytes = (k.len() + v.len()) as u64;
                let account = H256::from_slice(&k[1..33]);
                let path = Nibbles::from_hex(k[33..].to_vec());
                storages_to_write
                    .entry(account)
                    .or_default()
                    .push((path, v.into()));
                if flush_count.is_multiple_of(100_000) {
                    rt.block_on(
                        store.write_storage_trie_nodes_batch(storages_to_write.drain().collect()),
                    )?;
                }
                s.tick(1, item_bytes);
            }
            rt.block_on(store.write_storage_trie_nodes_batch(storages_to_write.drain().collect()))?;
            s.finish();
        }

        // ----- Stage 7: Diff layers (only if a journal exists) ---------
        let mut journal = Vec::new();
        match File::open(*GETH_JOURNAL_PATH) {
            Ok(mut file) => {
                file.read_to_end(&mut journal)?;
            }
            Err(error) => match error.kind() {
                ErrorKind::NotFound => {
                    debug!("Journal file not found, falling back to DB key.");
                    journal = gethdb
                        .state_db
                        .get(b"TrieJournal")
                        .unwrap()
                        .unwrap_or_default();
                }
                _ => error!("Error {error} while opening journal"),
            },
        };
        if journal_present || !journal.is_empty() {
            let mut s = runner.begin(
                "Diff layers",
                "layers",
                None,
                if journal.is_empty() {
                    None
                } else {
                    Some(journal.len() as u64)
                },
            );
            if journal.is_empty() {
                debug!("No journal found, skipping diff layers");
                s.finish_with_done(0);
            } else {
                let mut diffs = BTreeMap::new();
                let total_bytes = journal.len() as u64;
                let (version, mut rest) = u64::decode_unfinished(&journal)?;
                assert_eq!(version, 3);
                let disk_root;
                (disk_root, rest) = H256::decode_unfinished(rest)?;
                assert_eq!(disk_root, db_root_hash);
                (_, rest) = H256::decode_unfinished(rest)?;
                (_, rest) = u64::decode_unfinished(rest)?;
                let mut is_list;
                let disk_nodes_pl;
                (is_list, disk_nodes_pl, rest) = decode_rlp_item(rest)?;
                decode_nodes(disk_nodes_pl, &mut diffs)?;
                assert!(is_list);
                (_, rest) = bool::decode_unfinished(rest)?;
                (is_list, _, rest) = decode_rlp_item(rest)?;
                assert!(is_list);
                (is_list, _, rest) = decode_rlp_item(rest)?;
                assert!(is_list);

                let mut prev_remaining = rest.len();
                loop {
                    let (difflayer_root, difflayer_block, difflayer_nodes_pl);
                    debug!(rest = rest.len(), "Starting loop");
                    (difflayer_root, rest) = H256::decode_unfinished(rest)?;
                    (difflayer_block, rest) = u64::decode_unfinished(rest)?;
                    debug!(
                        difflayer_block,
                        difflayer_root = format!(
                            "{:032x}{:032x}",
                            u128::from_be_bytes(difflayer_root.0[..16].try_into().unwrap(),),
                            u128::from_be_bytes(difflayer_root.0[16..].try_into().unwrap())
                        ),
                        "Decoded difflayer header"
                    );
                    (is_list, difflayer_nodes_pl, rest) = decode_rlp_item(rest)?;
                    assert!(is_list);
                    decode_nodes(difflayer_nodes_pl, &mut diffs)?;
                    let (has_origin, _, next) = decode_rlp_item(rest)?;
                    if has_origin {
                        rest = next;
                    }
                    (_, rest) = bool::decode_unfinished(rest)?;
                    (is_list, _, rest) = decode_rlp_item(rest)?;
                    assert!(is_list);
                    (is_list, _, rest) = decode_rlp_item(rest)?;
                    assert!(is_list);
                    let (has_origin, _, next) = decode_rlp_item(rest)?;
                    if has_origin {
                        rest = next;
                        (is_list, _, rest) = decode_rlp_item(rest)?;
                        assert!(is_list);
                    }

                    let consumed = prev_remaining.saturating_sub(rest.len()) as u64;
                    s.tick(1, consumed);
                    prev_remaining = rest.len();

                    if difflayer_root == header.state_root {
                        debug!("found root");
                        assert!(
                            difflayer_block <= block_number,
                            "state root can never go back in time"
                        );
                        break;
                    }
                }
                // Account for header bytes consumed before the loop.
                let total_consumed = total_bytes.saturating_sub(rest.len() as u64);
                let already_counted = s.bytes_done;
                if total_consumed > already_counted {
                    s.tick(0, total_consumed - already_counted);
                }
                const ZERO: [u8; 32] = [0u8; 32];
                for ((owner, path), node) in diffs {
                    match owner {
                        ZERO => {
                            if let Ok(Node::Leaf(leaf)) = Node::decode(&node) {
                                let state = AccountState::decode(&leaf.value)?;
                                debug_assert_ne!(state.code_hash, H256::zero());
                                debug_assert_ne!(state.storage_root, H256::zero());
                                if state.code_hash != *EMPTY_KECCAK_HASH {
                                    codes.insert(state.code_hash);
                                }
                            }
                            state_trie
                                .db()
                                .put_batch(vec![(Nibbles::from_hex(path), node)])?
                        }
                        hashed_address => {
                            rt.block_on(store.write_storage_trie_nodes_batch(vec![(
                                H256::from_slice(&hashed_address),
                                vec![(Nibbles::from_hex(path), node)],
                            )]))?
                        }
                    }
                }
                s.finish();
            }
        }

        // ----- Stage 8: Account codes ----------------------------------
        {
            let mut s = runner.begin("Account codes", "codes", Some(codes.len() as u64), None);
            for code_hash in &codes {
                let code = gethdb
                    .read_code(code_hash.0)?
                    .ok_or_else(|| eyre::eyre!("missing code hash"))?;
                let bytes = code.len() as u64;
                rt.block_on(
                    store.add_account_code(Code::from_bytecode_unchecked(code.into(), *code_hash)),
                )?;
                s.tick(1, bytes);
            }
            s.finish();
        }

        // ----- Stage 9: FKV (optional) ---------------------------------
        if args.fkv {
            let mut s = runner.begin("Flat key/value", "ticks", Some(255), None);
            store.generate_flatkeyvalue()?;
            let done_marker = vec![0xff; FKV_COMPLETION_MARKER_LEN];
            let mut last_done: u64 = 0;
            while store.last_written()? != done_marker {
                std::thread::sleep(log_interval);
                let current = store.last_written()?;
                let estimated = current.first().copied().unwrap_or(0) as u64;
                if estimated > last_done {
                    s.tick(estimated - last_done, 0);
                    last_done = estimated;
                } else {
                    s.tick(0, 0);
                }
                debug!(?current, "FKV poll");
            }
            // FKV is done — bump to 100% for the final line.
            if last_done < 255 {
                s.tick(255 - last_done, 0);
            }
            s.finish();
        }
    }

    // ----- Stage 10: Final forkchoice + load initial state -------------
    {
        let s = runner.begin("Final forkchoice + load", "items", Some(2), None);
        rt.block_on(store.forkchoice_update(
            numbers_and_hashes,
            block_number,
            H256::from_slice(&block_hash),
            None,
            None,
        ))?;
        rt.block_on(store.load_initial_state())?;
        s.finish_with_done(2);
    }

    // ----- Stage 11: Validations (optional) ----------------------------
    if !args.skip_validation {
        // If we ran in validate_only mode we never counted leaves during
        // stage 5, so we don't have a total. The progress bar will just
        // show rate without %/ETA in that case.
        let total = if total_accounts > 0 {
            Some(total_accounts)
        } else {
            None
        };
        let mut s = runner.begin("Validations", "accounts", total, None);
        let state_root = header.state_root;
        info!("  Validating state trie with root {state_root:x}");
        store.open_locked_state_trie(state_root)?.validate()?;
        info!("  Validating storage tries and codes");
        for (hashed_address, account_state) in store
            .iter_accounts(state_root)
            .expect("Couldn't open state trie")
        {
            if account_state.storage_root != *EMPTY_TRIE_HASH {
                store
                    .open_locked_storage_trie(
                        hashed_address,
                        state_root,
                        account_state.storage_root,
                    )?
                    .validate()?;
            }
            if account_state.code_hash != *EMPTY_KECCAK_HASH {
                let code = store
                    .get_account_code(account_state.code_hash)?
                    .expect("inserted code not found");
                assert_eq!(account_state.code_hash, keccak(code.code()));
            }
            s.tick(1, 0);
        }
        info!("  Validating latest block");
        assert_eq!(store.get_latest_block_number().unwrap(), block_number);
        assert_eq!(
            rt.block_on(store.get_canonical_block_hash(block_number))
                .unwrap()
                .unwrap(),
            block_hash.into(),
        );
        assert_eq!(
            store.get_block_header(block_number).unwrap().unwrap(),
            header
        );
        assert!(store.has_state_root(header.state_root).unwrap());
        s.finish();
    }

    runner.print_summary();
    Ok(())
}

// ====================================================================
// Stage progress / ETA infrastructure
// ====================================================================

struct StageRecord {
    name: &'static str,
    elapsed: Duration,
    items: u64,
    unit: &'static str,
}

struct StageRunner {
    log_interval: Duration,
    total_stages: usize,
    next_idx: usize,
    started: Instant,
    records: Vec<StageRecord>,
}

impl StageRunner {
    fn new(total_stages: usize, log_interval: Duration) -> Self {
        info!(
            "geth2ethrex: {} stage(s), log interval {}s",
            total_stages,
            log_interval.as_secs()
        );
        Self {
            log_interval,
            total_stages,
            next_idx: 0,
            started: Instant::now(),
            records: Vec::new(),
        }
    }

    fn begin(
        &mut self,
        name: &'static str,
        unit: &'static str,
        total: Option<u64>,
        bytes_total: Option<u64>,
    ) -> StageProgress<'_> {
        self.next_idx += 1;
        let idx = self.next_idx;
        let total_str = match (total, bytes_total) {
            (Some(t), _) => format!("{} {}", human_count(t), unit),
            (_, Some(b)) if b > 0 => format!("~{}", human_bytes(b)),
            _ => "?".to_string(),
        };
        info!(
            ">>> Stage {}/{} — {} (total: {})",
            idx, self.total_stages, name, total_str
        );
        let now = Instant::now();
        StageProgress {
            stage_idx: idx,
            stage_count: self.total_stages,
            log_interval: self.log_interval,
            runner: self,
            name,
            unit,
            started: now,
            last_log: now,
            last_tick_time: now,
            window_start: now,
            window_start_done: 0,
            done: 0,
            total,
            bytes_done: 0,
            bytes_total,
        }
    }

    fn print_summary(&self) {
        info!("");
        info!("══════════════════ Migration summary ══════════════════");
        for r in &self.records {
            info!(
                "  {:32} {:>12}   {:>12} {}",
                r.name,
                human_duration(r.elapsed),
                human_count(r.items),
                r.unit
            );
        }
        info!(
            "  {:32} {:>12}",
            "TOTAL",
            human_duration(self.started.elapsed())
        );
        info!("═══════════════════════════════════════════════════════");
    }
}

struct StageProgress<'a> {
    runner: &'a mut StageRunner,
    stage_idx: usize,
    stage_count: usize,
    name: &'static str,
    unit: &'static str,
    started: Instant,
    last_log: Instant,
    last_tick_time: Instant,
    window_start: Instant,
    window_start_done: u64,
    log_interval: Duration,
    done: u64,
    total: Option<u64>,
    bytes_done: u64,
    bytes_total: Option<u64>,
}

impl StageProgress<'_> {
    fn tick(&mut self, items: u64, bytes: u64) {
        let now = Instant::now();
        // If ticks have stopped firing for a while (e.g. a long blocking
        // operation between ticks like state trie validation), the
        // since-stage-start average rate becomes meaningless. Reset the
        // rate window so subsequent rate/ETA reflect real throughput.
        if now.duration_since(self.last_tick_time) > self.log_interval * 2 {
            self.window_start = now;
            self.window_start_done = self.done;
        }
        self.last_tick_time = now;
        self.done = self.done.saturating_add(items);
        self.bytes_done = self.bytes_done.saturating_add(bytes);
        if self.last_log.elapsed() >= self.log_interval {
            self.log_progress();
            self.last_log = now;
        }
    }

    /// Rate and ETA based on the current observation window. Returns
    /// `None` when the window is too young or done==window_start_done,
    /// so callers can render `—` instead of an unstable estimate.
    fn windowed_rate(&self) -> Option<f64> {
        let window_elapsed = self.window_start.elapsed();
        // Need at least one full log interval of observations to trust
        // the rate. This is what suppresses the giant ETA on the first
        // post-reset tick.
        if window_elapsed < self.log_interval {
            return None;
        }
        let window_done = self.done.saturating_sub(self.window_start_done);
        if window_done == 0 {
            return None;
        }
        let secs = window_elapsed.as_secs_f64().max(0.001);
        Some(window_done as f64 / secs)
    }

    fn log_progress(&self) {
        let elapsed = self.started.elapsed();
        let rate = self.windowed_rate();

        let progress_str = if let Some(t) = self.total.filter(|t| *t > 0) {
            let p = ((self.done as f64 / t as f64) * 100.0).min(100.0);
            let bytes_part = if self.bytes_done > 0 {
                format!(", {}", human_bytes(self.bytes_done))
            } else {
                String::new()
            };
            format!(
                "{} / {} {} ({:.1}%{})",
                human_count(self.done),
                human_count(t),
                self.unit,
                p,
                bytes_part
            )
        } else if let Some(bt) = self.bytes_total.filter(|b| *b > 0) {
            let p = ((self.bytes_done as f64 / bt as f64) * 100.0).min(100.0);
            format!(
                "{} {} ({} / {}, {:.1}%)",
                human_count(self.done),
                self.unit,
                human_bytes(self.bytes_done),
                human_bytes(bt),
                p
            )
        } else if self.bytes_done > 0 {
            format!(
                "{} {} ({})",
                human_count(self.done),
                self.unit,
                human_bytes(self.bytes_done)
            )
        } else {
            format!("{} {}", human_count(self.done), self.unit)
        };

        let rate_str = match rate {
            Some(r) => format!("{}/s", human_count(r.round() as u64)),
            None => "—/s".to_string(),
        };

        info!(
            "  Stage {}/{} — {} | {} | {} | elapsed {} | ETA {}",
            self.stage_idx,
            self.stage_count,
            self.name,
            progress_str,
            rate_str,
            human_duration(elapsed),
            self.eta_string()
        );
    }

    fn eta_string(&self) -> String {
        let Some(rate) = self.windowed_rate() else {
            return "—".to_string();
        };
        if let Some(t) = self.total
            && t > self.done
        {
            let eta_secs = (t - self.done) as f64 / rate;
            return human_duration(Duration::from_secs_f64(eta_secs.max(0.0)));
        }
        if let Some(bt) = self.bytes_total
            && bt > self.bytes_done
        {
            // Diff layers track raw journal bytes (uncompressed), so the
            // bytes-based ETA is meaningful here.
            let bytes_rate = self.bytes_rate();
            if bytes_rate > 0.0 {
                let eta_secs = (bt - self.bytes_done) as f64 / bytes_rate;
                return human_duration(Duration::from_secs_f64(eta_secs.max(0.0)));
            }
        }
        "?".to_string()
    }

    fn bytes_rate(&self) -> f64 {
        let secs = self.window_start.elapsed().as_secs_f64().max(0.001);
        // Bytes don't have their own window-start counter; for the small
        // stages that drive ETA off bytes (diff layers) the gap between
        // ticks is always small, so lifetime bytes/elapsed is fine.
        self.bytes_done as f64 / secs
    }

    fn finish(self) {
        let elapsed = self.started.elapsed();
        info!(
            "<<< Stage {}/{} — {} done in {} ({} {})",
            self.stage_idx,
            self.stage_count,
            self.name,
            human_duration(elapsed),
            human_count(self.done),
            self.unit
        );
        self.runner.records.push(StageRecord {
            name: self.name,
            elapsed,
            items: self.done,
            unit: self.unit,
        });
    }

    /// Convenience for stages with a fixed item count where we don't tick
    /// item-by-item.
    fn finish_with_done(mut self, done: u64) {
        self.done = done;
        self.finish();
    }
}

fn plan_stage_count(args: &Args, journal_present: bool) -> usize {
    let mut n = 2; // genesis init + read metadata, always
    if !args.validate_only {
        n += 4; // headers+bodies, canonical, state trie, storage tries
        if journal_present {
            n += 1; // diff layers
        }
        n += 1; // account codes
        if args.fkv {
            n += 1; // FKV
        }
    }
    n += 1; // final forkchoice + load
    if !args.skip_validation {
        n += 1; // validations
    }
    n
}

/// Returns the approximate on-disk byte size of the keys in `[start, end)`.
/// Falls back to 0 when the underlying RocksDB call doesn't return a value
/// (e.g. an empty range).
fn approx_size(db: &DBWithThreadMode<SingleThreaded>, start: &[u8], end: &[u8]) -> u64 {
    db.get_approximate_sizes(&[Range::new(start, end)])
        .first()
        .copied()
        .unwrap_or(0)
}

/// Estimates the number of keys whose key falls in `[start, end)`.
///
/// RocksDB exposes `estimate-num-keys` for the entire DB and approximate
/// on-disk byte sizes per key range. We split the per-CF key estimate by
/// the size ratio of the prefix range to the whole DB. This is a rough
/// estimate but stays linear in real progress, unlike comparing
/// uncompressed key+value bytes against a compressed SST size estimate.
fn approx_key_count(db: &DBWithThreadMode<SingleThreaded>, start: &[u8], end: &[u8]) -> u64 {
    static MAX_KEY: [u8; 64] = [0xff_u8; 64];
    let prefix_size = approx_size(db, start, end);
    let total_size = approx_size(db, b"", &MAX_KEY);
    let total_keys = db
        .property_int_value("rocksdb.estimate-num-keys")
        .ok()
        .flatten()
        .unwrap_or(0);
    if total_size == 0 || prefix_size == 0 {
        return 0;
    }
    ((total_keys as u128 * prefix_size as u128) / total_size as u128) as u64
}

fn human_count(n: u64) -> String {
    let s = n.to_string();
    let bytes = s.as_bytes();
    let mut out = String::with_capacity(s.len() + s.len() / 3);
    for (i, b) in bytes.iter().enumerate() {
        let from_end = bytes.len() - i;
        if i > 0 && from_end.is_multiple_of(3) {
            out.push(',');
        }
        out.push(*b as char);
    }
    out
}

fn human_bytes(n: u64) -> String {
    const UNITS: &[&str] = &["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
    if n < 1024 {
        return format!("{n} B");
    }
    let mut v = n as f64;
    let mut i = 0;
    while v >= 1024.0 && i < UNITS.len() - 1 {
        v /= 1024.0;
        i += 1;
    }
    format!("{:.2} {}", v, UNITS[i])
}

fn human_duration(d: Duration) -> String {
    let total = d.as_secs();
    let days = total / 86_400;
    let hh = (total % 86_400) / 3_600;
    let mm = (total % 3_600) / 60;
    let ss = total % 60;
    if days > 0 {
        format!("{days}d {hh:02}:{mm:02}:{ss:02}")
    } else {
        format!("{hh:02}:{mm:02}:{ss:02}")
    }
}

// ====================================================================
// Geth DB access (unchanged)
// ====================================================================

struct GethDB {
    ancient_db_path: PathBuf,
    state_db: DBWithThreadMode<SingleThreaded>,
}
impl GethDB {
    pub fn open() -> eyre::Result<Self> {
        let ancient_db_path = AsRef::<Path>::as_ref(*GETH_DB_PATH).join("ancient/chain");
        let state_db =
            DBWithThreadMode::open_for_read_only(&Options::default(), *GETH_DB_PATH, false)?;
        Ok(Self {
            ancient_db_path,
            state_db,
        })
    }

    fn read_from_freezer_table(
        &self,
        name: &str,
        compressed: bool,
        first: u64,
        last: u64,
    ) -> eyre::Result<Vec<Vec<u8>>> {
        let idx_path = self.ancient_db_path.join(format!(
            "{name}.{}",
            if compressed { "cidx" } else { "ridx" }
        ));
        let index_file = File::open(&idx_path)?;
        let size = index_file.metadata()?.size();
        if first * 6 >= size {
            return Ok(Vec::new());
        }
        let to_read = if last * 6 >= size {
            (size - first * 6) as usize
        } else {
            ((last - first + 2) * 6) as usize
        };
        let mut index_buf = vec![0; to_read];
        index_file.read_exact_at(&mut index_buf, 6 * first)?;
        let index_entries: Vec<_> = index_buf
            .chunks_exact(6)
            .map(|entry_bytes| {
                let fnum_bytes = entry_bytes[..2].try_into().unwrap();
                let offs_bytes = entry_bytes[2..].try_into().unwrap();
                (
                    u16::from_be_bytes(fnum_bytes),
                    u32::from_be_bytes(offs_bytes),
                )
            })
            .collect();
        if !index_entries.is_sorted() {
            eyre::bail!("broken index")
        }
        let (Some((first_file, _)), Some((last_file, _))) = (
            index_entries.first().copied(),
            index_entries.last().copied(),
        ) else {
            return Ok(Vec::new());
        };
        let data_files: Vec<_> = (first_file..=last_file)
            .map(|f| {
                File::open(self.ancient_db_path.join(format!(
                    "{name}.{f:04}.{}",
                    if compressed { "cdat" } else { "rdat" }
                )))
            })
            .collect::<io::Result<Vec<_>>>()?;
        let mut data = Vec::with_capacity((last - first + 1) as usize);
        let (mut pnum, mut poff) = index_entries[0];
        for (fnum, offset) in index_entries.into_iter().skip(1) {
            if pnum != fnum {
                poff = 0;
            }
            let file = &data_files[(fnum - first_file) as usize];
            let mut entry_data = vec![0; (offset - poff) as usize];
            file.read_exact_at(&mut entry_data, poff as u64)?;
            if compressed {
                entry_data = snap::raw::Decoder::new().decompress_vec(&entry_data)?;
            }
            (pnum, poff) = (fnum, offset);
            data.push(entry_data);
        }
        Ok(data)
    }

    fn try_read_hashes_from_freezer(&self, first: u64, last: u64) -> eyre::Result<Vec<[u8; 32]>> {
        let hashes_vecs = self.read_from_freezer_table("hashes", false, first, last)?;
        hashes_vecs
            .into_iter()
            .map(|h| <[u8; 32]>::try_from(h.as_slice()))
            .collect::<Result<Vec<_>, _>>()
            .map_err(|_| eyre::eyre!("bad length"))
    }
    fn try_read_hashes_from_statedb(&self, first: u64, last: u64) -> eyre::Result<Vec<[u8; 32]>> {
        let keys = (first..=last).map(|i| [&b"h"[..], &i.to_be_bytes(), b"n"].concat());
        let mut values = Vec::with_capacity((last - first + 1) as usize);
        for hash in self.state_db.multi_get(keys) {
            let Some(hash) = hash? else {
                continue;
            };
            values.push(hash.try_into().unwrap())
        }
        Ok(values)
    }

    fn try_read_block_from_freezer(&self, block_num: u64) -> eyre::Result<[Option<Vec<u8>>; 2]> {
        let mut header = self.read_from_freezer_table("headers", true, block_num, block_num)?;
        let mut body = self.read_from_freezer_table("bodies", true, block_num, block_num)?;
        Ok([header.drain(..).next(), body.drain(..).next()])
    }
    fn try_read_block_from_statedb(
        &self,
        block_num: u64,
        block_hash: [u8; 32],
        key_prefixes: &[u8],
    ) -> eyre::Result<[Option<Vec<u8>>; 2]> {
        let keys = key_prefixes
            .iter()
            .map(|p| [&[*p][..], &block_num.to_be_bytes(), &block_hash].concat());
        let mut values = self.state_db.multi_get(keys);
        debug_assert_eq!(values.len(), 2);
        if values.len() != 2 {
            eyre::bail!("rocksdb returned an unexpected number of results");
        }
        let mut values = values.drain(..);
        Ok([values.next().unwrap()?, values.next().unwrap()?])
    }

    pub fn read_hashes_from_gethdb(&self, first: u64, last: u64) -> eyre::Result<Vec<[u8; 32]>> {
        let frozen_hashes = self.try_read_hashes_from_freezer(first, last)?;
        let state_hashes = if last - first + 1 != frozen_hashes.len() as u64 {
            self.try_read_hashes_from_statedb(first + frozen_hashes.len() as u64, last)?
        } else {
            Vec::new()
        };
        Ok([frozen_hashes, state_hashes].concat())
    }
    pub fn read_block_from_gethdb(
        &self,
        block_num: u64,
        block_hash: [u8; 32],
    ) -> eyre::Result<[Vec<u8>; 2]> {
        match self.try_read_block_from_freezer(block_num)? {
            [None, None] => {
                let [Some(header), Some(body)] =
                    self.try_read_block_from_statedb(block_num, block_hash, b"hb")?
                else {
                    eyre::bail!("missing body and/or header")
                };
                Ok([header, body])
            }
            [Some(header), None] => {
                let [None, Some(body)] =
                    self.try_read_block_from_statedb(block_num, block_hash, b"b")?
                else {
                    eyre::bail!("missing body")
                };
                Ok([header, body])
            }
            [None, Some(body)] => {
                let [Some(header), None] =
                    self.try_read_block_from_statedb(block_num, block_hash, b"h")?
                else {
                    eyre::bail!("missing header")
                };
                Ok([header, body])
            }
            [Some(header), Some(body)] => Ok([header, body]),
        }
    }

    pub fn read_code(&self, code_hash: [u8; 32]) -> Result<Option<Vec<u8>>, rocksdb::Error> {
        self.state_db.get([&b"c"[..], &code_hash].concat())
    }

    pub fn is_path_based(&self) -> eyre::Result<bool> {
        Ok(self.state_db.get(b"A")?.is_some())
    }
}

fn decode_nodes(
    buf: &[u8],
    diffs: &mut BTreeMap<([u8; 32], Vec<u8>), Vec<u8>>,
) -> eyre::Result<()> {
    let mut is_list;
    let mut remaining_nodes = buf;
    let mut journal_node;
    while !remaining_nodes.is_empty() {
        (is_list, journal_node, remaining_nodes) = decode_rlp_item(remaining_nodes)?;
        assert!(is_list);
        let (owner, nodes) = H256::decode_unfinished(journal_node)?;
        let (is_list, mut node_list, after) = decode_rlp_item(nodes)?;
        assert!(is_list);
        assert!(after.is_empty());
        while !node_list.is_empty() {
            let (is_list, pathnode, path, node);
            (is_list, pathnode, node_list) = decode_rlp_item(node_list)?;
            assert!(is_list);
            (path, node) = decode_bytes(pathnode)?;
            let (node, after) = decode_bytes(node)?;
            assert!(after.is_empty());
            diffs.insert((owner.0, path.to_vec()), node.to_vec());
        }
    }
    Ok(())
}

#[derive(Parser)]
struct Args {
    #[arg(
        required = true,
        value_name = "NUMBER",
        help = "Block number to sync to"
    )]
    block_number: BlockNumber,
    #[arg(
        required = true,
        long = "input_dir",
        value_name = "INPUT_DIRECTORY",
        help = "Receives the name of the directory where the State Dump will be read from."
    )]
    pub input_dir: String,
    #[arg(
        required = true,
        long = "output_dir",
        value_name = "OUTPUT_DIRECTORY",
        help = "Receives the name of the directory where the State Dump will be written to."
    )]
    pub output_dir: String,
    #[arg(
        short = 'v',
        long = "validate_only",
        help = "If true, runs only validations on Ethrex DB",
        default_value = "false"
    )]
    pub validate_only: bool,
    #[arg(
        long = "network",
        value_name = "GENESIS_FILE_PATH",
        help = "Receives a `Genesis` struct in json format. You can look at some example genesis files at `fixtures/genesis/*`.",
        long_help = "Alternatively, the name of a known network can be provided instead to use its preset genesis file and include its preset bootnodes. The networks currently supported include holesky, sepolia, hoodi and mainnet. If not specified, defaults to mainnet.",
        help_heading = "Node options",
        env = "ETHREX_NETWORK",
        value_parser = clap::value_parser!(Network),
    )]
    pub network: Network,
    #[arg(
        long = "flatkeyvalue",
        help = "If true, generates FlatKeyValues",
        default_value = "false"
    )]
    pub fkv: bool,
    #[arg(
        long = "skip-validation",
        help = "If true, skips validation of the migrated data",
        default_value = "false"
    )]
    pub skip_validation: bool,
    #[arg(
        long = "log-interval",
        value_name = "SECONDS",
        help = "How often to emit a per-stage progress line.",
        default_value = "30"
    )]
    pub log_interval: u64,
}
