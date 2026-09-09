use crate::db;
use crate::{domain::NewRecord, query::ContextRequest};
use serde::Serialize;
use serde_json::{json, Value};
use std::fs::{File, OpenOptions};
use std::path::{Path, PathBuf};
use uuid::Uuid;

const MAX_BATCH_RECORDS: usize = 64;
const MAX_BATCH_BYTES: usize = 2 * 1024 * 1024;

#[derive(Debug, Default, Serialize)]
pub struct Flush {
    pub sent: usize,
    pub rejected: usize,
    pub pending: usize,
}
pub struct Client {
    http: reqwest::Client,
    url: String,
    token: String,
    outbox: Option<PathBuf>,
}
impl Client {
    pub fn new(url: &str, token: &str) -> anyhow::Result<Self> {
        anyhow::ensure!(!url.trim().is_empty(), "url is required");
        anyhow::ensure!(!token.trim().is_empty(), "token is required");
        Ok(Self {
            http: reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(240))
                .redirect(reqwest::redirect::Policy::none())
                .build()?,
            url: url.trim_end_matches('/').to_owned(),
            token: token.to_owned(),
            outbox: None,
        })
    }
    pub fn with_outbox(mut self, root: &Path) -> anyhow::Result<Self> {
        let identity = db::hash(&format!("{}\n{}", self.url, self.token));
        let path = root.join(identity);
        std::fs::create_dir_all(&path)?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700))?;
        }
        self.outbox = Some(path);
        Ok(self)
    }
    pub async fn capture(&self, records: &[NewRecord]) -> anyhow::Result<Value> {
        anyhow::ensure!(!records.is_empty(), "at least one record is required");
        let batches = batches(records)?;
        let batch_count = batches.len();
        if let Some(outbox) = &self.outbox {
            let _lock = lock_outbox(outbox).await?;
            for body in batches {
                enqueue(outbox, &body)?;
            }
            return Ok(serde_json::to_value(self.flush_locked(outbox).await?)?);
        }
        let mut sent = 0;
        let mut response = Value::Null;
        for body in batches {
            response = self.request("/v1/records", &body).await?;
            sent += record_count(&body)?;
        }
        if batch_count == 1 {
            Ok(response)
        } else {
            Ok(json!({"sent":sent,"rejected":0,"pending":0}))
        }
    }
    pub async fn context(&self, request: &ContextRequest) -> anyhow::Result<Value> {
        self.request("/v1/context", &serde_json::to_value(request)?)
            .await
    }
    pub async fn get_record(&self, id: Uuid) -> anyhow::Result<Value> {
        self.get(&format!("/v1/records/{id}")).await
    }
    async fn request(&self, path: &str, body: &Value) -> anyhow::Result<Value> {
        let r = self
            .http
            .post(format!("{}{}", self.url, path))
            .bearer_auth(&self.token)
            .json(body)
            .send()
            .await?;
        let s = r.status();
        anyhow::ensure!(s.is_success(), "ContextMesh HTTP {s}");
        Ok(r.json().await?)
    }
    async fn get(&self, path: &str) -> anyhow::Result<Value> {
        let r = self
            .http
            .get(format!("{}{}", self.url, path))
            .bearer_auth(&self.token)
            .send()
            .await?;
        let s = r.status();
        anyhow::ensure!(s.is_success(), "ContextMesh HTTP {s}");
        Ok(r.json().await?)
    }
    pub async fn flush(&self) -> anyhow::Result<Flush> {
        let Some(outbox) = &self.outbox else {
            return Ok(Flush::default());
        };
        let _lock = lock_outbox(outbox).await?;
        self.flush_locked(outbox).await
    }
    async fn flush_locked(&self, outbox: &Path) -> anyhow::Result<Flush> {
        let mut files: Vec<_> = std::fs::read_dir(outbox)?
            .filter_map(Result::ok)
            .map(|e| e.path())
            .filter(|p| p.extension().is_some_and(|x| x == "pending"))
            .collect();
        files.sort();
        let mut sum = Flush::default();
        for (i, path) in files.iter().enumerate() {
            if i >= 100 {
                sum.pending += pending_count(&files[i..])?;
                break;
            }
            let bytes = match std::fs::read(path) {
                Ok(b) => b,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
                Err(e) => return Err(e.into()),
            };
            let body = serde_json::from_slice::<Value>(&bytes)?;
            let count = record_count(&body)?;
            match self
                .http
                .post(format!("{}/v1/records", self.url))
                .bearer_auth(&self.token)
                .json(&body)
                .timeout(std::time::Duration::from_secs(30))
                .send()
                .await
            {
                Ok(r) if r.status().is_success() => {
                    std::fs::remove_file(path).or_else(|e| {
                        if e.kind() == std::io::ErrorKind::NotFound {
                            Ok(())
                        } else {
                            Err(e)
                        }
                    })?;
                    sum.sent += count
                }
                Ok(r) => {
                    let status = r.status();
                    let error = if r.content_length().unwrap_or(0) <= 64 * 1024 {
                        r.json::<Value>()
                            .await
                            .ok()
                            .and_then(|v| v.get("error").and_then(Value::as_str).map(str::to_owned))
                    } else {
                        None
                    };
                    if permanent(status.as_u16(), error.as_deref()) {
                        std::fs::rename(path, path.with_extension("rejected"))?;
                        sum.rejected += count;
                    } else {
                        sum.pending += count + pending_count(&files[i + 1..])?;
                        break;
                    }
                }
                Err(_) => {
                    sum.pending += count + pending_count(&files[i + 1..])?;
                    break;
                }
            }
        }
        Ok(sum)
    }
}

fn batches(records: &[NewRecord]) -> anyhow::Result<Vec<Value>> {
    let mut batches = Vec::new();
    let mut current = Vec::new();
    for record in records {
        let mut candidate = current.clone();
        candidate.push(record);
        let body = json!({"records":candidate});
        if candidate.len() > MAX_BATCH_RECORDS || serde_json::to_vec(&body)?.len() > MAX_BATCH_BYTES
        {
            anyhow::ensure!(!current.is_empty(), "record exceeds request body limit");
            batches.push(json!({"records":current}));
            current = vec![record];
            anyhow::ensure!(
                serde_json::to_vec(&json!({"records":current}))?.len() <= MAX_BATCH_BYTES,
                "record exceeds request body limit"
            );
        } else {
            current = candidate;
        }
    }
    if !current.is_empty() {
        batches.push(json!({"records":current}));
    }
    Ok(batches)
}

fn enqueue(outbox: &Path, body: &Value) -> anyhow::Result<()> {
    let bytes = serde_json::to_vec(body)?;
    let key = format!(
        "{:020}-{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_nanos(),
        db::hash(&String::from_utf8_lossy(&bytes))
    );
    let temp = outbox.join(format!("{}.tmp", Uuid::new_v4()));
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    {
        use std::io::Write;
        let mut file = options.open(&temp)?;
        file.write_all(&bytes)?;
        file.sync_all()?;
    }
    std::fs::rename(temp, outbox.join(format!("{key}.pending")))?;
    #[cfg(unix)]
    File::open(outbox)?.sync_all()?;
    Ok(())
}

async fn lock_outbox(outbox: &Path) -> anyhow::Result<File> {
    let path = outbox.join("flush.lock");
    tokio::task::spawn_blocking(move || {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path)?;
        file.lock()?;
        Ok(file)
    })
    .await?
}

fn record_count(body: &Value) -> anyhow::Result<usize> {
    body.get("records")
        .and_then(Value::as_array)
        .map(Vec::len)
        .ok_or_else(|| anyhow::anyhow!("invalid outbox entry"))
}

fn pending_count(paths: &[PathBuf]) -> anyhow::Result<usize> {
    paths.iter().try_fold(0, |sum, path| {
        let body: Value = serde_json::from_slice(&std::fs::read(path)?)?;
        Ok(sum + record_count(&body)?)
    })
}

fn permanent(status: u16, error: Option<&str>) -> bool {
    if matches!(
        error,
        Some("context_changed_retry" | "invalid_input" | "input_unavailable")
    ) {
        return false;
    }
    matches!(status, 400 | 401 | 403 | 409 | 410 | 413 | 422)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::domain::Scope;

    fn record(content: String) -> NewRecord {
        NewRecord {
            id: Uuid::new_v4(),
            content,
            scope: Scope::default(),
            inputs: vec![],
            supports: vec![],
            supersedes: vec![],
            metadata: json!({}),
        }
    }

    #[test]
    fn batches_bound_count_and_serialized_bytes() {
        let records: Vec<_> = (0..65).map(|_| record("x".into())).collect();
        let split = batches(&records).unwrap();
        assert_eq!(split.len(), 2);
        assert_eq!(record_count(&split[0]).unwrap(), 64);
        assert_eq!(record_count(&split[1]).unwrap(), 1);

        let records: Vec<_> = (0..64).map(|_| record("x".repeat(40_000))).collect();
        let split = batches(&records).unwrap();
        assert!(split.len() > 1);
        assert!(split
            .iter()
            .all(|body| serde_json::to_vec(body).unwrap().len() <= MAX_BATCH_BYTES));
        assert_eq!(
            split
                .iter()
                .map(|body| record_count(body).unwrap())
                .sum::<usize>(),
            64
        );
    }

    #[test]
    fn retryable_errors_remain_pending() {
        assert!(!permanent(409, Some("context_changed_retry")));
        assert!(!permanent(400, Some("invalid_input")));
        assert!(!permanent(404, Some("not_found")));
        assert!(permanent(409, Some("record_id_conflict")));
        assert!(permanent(422, Some("invalid_record")));
    }

    #[tokio::test(flavor = "current_thread")]
    async fn outbox_lock_wait_does_not_block_runtime() {
        let directory = std::env::temp_dir().join(format!("contextmesh-outbox-{}", Uuid::new_v4()));
        std::fs::create_dir(&directory).unwrap();
        let first = lock_outbox(&directory).await.unwrap();
        let waiting_directory = directory.clone();
        let waiter = tokio::spawn(async move { lock_outbox(&waiting_directory).await.unwrap() });

        tokio::task::yield_now().await;
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        assert!(!waiter.is_finished());
        drop(first);
        let second = tokio::time::timeout(std::time::Duration::from_secs(1), waiter)
            .await
            .expect("waiter should progress after the first guard is released")
            .unwrap();
        drop(second);
        std::fs::remove_dir_all(directory).unwrap();
    }
}
