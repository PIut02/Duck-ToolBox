//! 运行期定位 bionic 的属性后端结构，用于「私有副本」。
//!
//! 任一环节失败都返回 `Err`，跳过属性内存修改并记录原因。

use std::collections::HashSet;

use anyhow::bail;
use log::info;

/// 属性区映射路径前缀。
const PROP_PREFIX: &str = "/dev/__properties__/";

/// 退化锚点无法定位所属映射时的默认搜索窗口。
const SCAN_WINDOW: usize = 0x400;

/// `ContextNode` 元素大小的候选值（随 libc 版本变，逐个验证）。
const STRIDE_CANDIDATES: &[usize] = &[0x28, 0x20, 0x30, 0x18, 0x38, 0x40, 0x48, 0x50];

/// 探测「某个 key 实际被路由到哪个区」用的 key（`ro.*` 一定存在）。
const PROBE_KEYS: &[&str] = &[
    "ro.build.id",
    "ro.product.model",
    "ro.hardware",
    "ro.build.version.sdk",
];

// ── /proc/self/maps ──────────────────────────────────────────────────────────

mod maps;
use maps::Maps;

// ── 裸内存读写 ───────────────────────────────────────────────────────────────

/// # Safety
/// `addr` 必须落在可读映射内且长度足够（调用方用 `Maps::readable` 校验）。
/// 使用非对齐读，不要求 8 字节对齐。
unsafe fn read_u64(addr: usize) -> u64 {
    unsafe { (addr as *const u64).read_unaligned() }
}

/// # Safety
/// `addr` 必须落在**可写**映射内。
unsafe fn write_u64(addr: usize, value: u64) {
    unsafe { (addr as *mut u64).write_unaligned(value) }
}

// ── 结构定位 ─────────────────────────────────────────────────────────────────

/// 已解析出的迁移点。
pub struct PropBackend {
    /// `ContextNode` 数组基址
    contexts: usize,
    /// 数组元素个数（`num_contexts_`）
    count: usize,
    /// 单个 `ContextNode` 的大小
    stride: usize,
    /// `ContextNode::prop_area_` 在元素内的偏移
    pa_off: usize,
}

/// 进程内缓存：属性后端结构解析一次即可。
///
/// 必须在第一次 COW 克隆**之前**解析——克隆会把 context 节点的 `prop_area_`
/// 改成匿名副本，之后 `validate_context_array` 的「非零节点必须落在真实
/// `/dev/__properties__` 映射里」校验就会失败（真机实测报
/// `no self-consistent context array in backend`）。
static BACKEND: std::sync::OnceLock<PropBackend> = std::sync::OnceLock::new();

impl PropBackend {
    /// 带缓存的解析（成功结果在进程内复用）。
    pub fn resolve_cached() -> anyhow::Result<&'static Self> {
        if let Some(b) = BACKEND.get() {
            return Ok(b);
        }
        let b = Self::resolve()?;
        Ok(BACKEND.get_or_init(|| b))
    }

    /// 运行期解析 bionic 属性后端。失败时返回 `Err`，调用方应降级。
    pub fn resolve() -> anyhow::Result<Self> {
        let maps = Maps::load();
        if maps.regions.is_empty() {
            bail!("/proc/self/maps unreadable");
        }

        let wrapper =
            unsafe { libc::dlsym(libc::RTLD_DEFAULT, c"__system_property_find".as_ptr()) as usize };
        if wrapper == 0 {
            bail!("dlsym(__system_property_find) failed");
        }

        let area_starts = maps.prop_area_starts();
        if area_starts.len() < 8 {
            bail!(
                "too few /dev/__properties__ mappings ({})",
                area_starts.len()
            );
        }
        let probe = probe_area_start(wrapper, &maps);
        info!(
            "prop backend: {} prop areas, probe route = {:#x?}",
            area_starts.len(),
            probe
        );

        let array = locate_context_array(&maps, &area_starts, probe)?;
        info!(
            "prop backend: contexts={:#x} count={} stride={:#x} pa_off={:#x}",
            array.base, array.count, array.stride, array.pa_off
        );
        Ok(Self {
            contexts: array.base,
            count: array.count,
            stride: array.stride,
            pa_off: array.pa_off,
        })
    }

    /// 是否有 context 节点引用了这个属性区。
    ///
    /// 用来在建副本**之前**判断迁移是否有意义：同一个文件在本进程里可能被映射
    /// 多次（活跃/非活跃两套 `ContextsSerialized` 各自映射一遍），只有被节点
    /// 引用的那一份才值得克隆 —— 否则克隆出来也没有指针指向它，纯属浪费
    /// （还会白白 mmap/munmap 一轮，扰动地址空间）。
    pub fn references_area(&self, addr: usize) -> bool {
        (0..self.count).any(|i| {
            let slot = self.contexts + i * self.stride + self.pa_off;
            (unsafe { read_u64(slot) }) as usize == addr
        })
    }

    /// `ContextNode::context_` 字段地址。
    ///
    /// bionic 布局（`system_properties/include/system_properties/context_node.h`）：
    /// `Lock lock_`(+0x00) / `const char* context_`(+0x08) / `prop_area* pa_`(+0x10) /
    /// `bool no_access_`(+0x18) / `const char* filename_`(+0x20)，stride 0x28。
    /// 故 `context_ = pa_off - 8`。
    fn context_slot(&self, idx: usize) -> usize {
        self.contexts + idx * self.stride + self.pa_off.saturating_sub(8)
    }

    /// 改装第 `idx` 个节点保存的 context 名指针。
    ///
    /// bionic specialize 之后会跑 `SystemProperties::Init()`（`initialized_` 已为 true
    /// 的分支）→ `ContextsSerialized::ResetAccess()` → 每个节点
    /// `ContextNode::ResetAccess()`：`access("<props_dir>/<context_>", R_OK)` 失败就
    /// `Unmap()` 掉 `prop_area_` 并把 `no_access_` 置真。
    ///
    /// 所以把不可读 context 的 `context_` 换成 app 域可读的 context 名（`default_prop`），
    /// 复查即可通过、我们挂上去的私有区/合成区不会被回收；属性**路由**由 property_info
    /// 的 entry 表决定（名字 → context 下标），本次改动不碰路由，因此读取路径不变。
    pub fn set_node_context(&self, idx: usize, ptr: usize) {
        if idx >= self.count {
            return;
        }
        unsafe { write_u64(self.context_slot(idx), ptr as u64) };
    }

    /// 把所有 `prop_area_ == from` 的节点改指向 `to`，并把它们的 `context_`
    /// 改指向 `label`（app 可读的 context 名字符串，由调用方保证活到进程结束）。
    /// 返回改写个数。
    pub fn repoint_area_and_relabel(&self, from: usize, to: usize, label: usize) -> usize {
        let mut n = 0;
        for i in 0..self.count {
            let slot = self.contexts + i * self.stride + self.pa_off;
            if (unsafe { read_u64(slot) }) as usize == from {
                unsafe { write_u64(slot, to as u64) };
                self.set_node_context(i, label);
                n += 1;
            }
        }
        n
    }

    /// context 节点总数（`num_contexts_`）。
    pub fn node_count(&self) -> usize {
        self.count
    }

    /// 读取第 `idx` 个 context 节点的 `prop_area_`；0 表示该 context 的属性区
    /// **尚未映射进本进程**（惰性打开失败 / SELinux 拒绝 / 从未被查询）。
    pub fn node_area(&self, idx: usize) -> usize {
        if idx >= self.count {
            return 0;
        }
        let slot = self.contexts + idx * self.stride + self.pa_off;
        (unsafe { read_u64(slot) }) as usize
    }

    /// 把第 `idx` 个 context 节点的 `prop_area_` 改成 `addr`（0 = 清空）。
    ///
    /// 用于给「永不映射」的 context（app 域被 SELinux 拒绝的属性区）挂上进程内
    /// 合成的匿名属性区：挂上之后该 context 的读取走合成区，真实区文件完全不碰。
    /// 调用方必须保证 `addr` 指向一个内容自洽且活到进程结束的 `prop_area`。
    pub fn set_node_area(&self, idx: usize, addr: usize) {
        if idx >= self.count {
            return;
        }
        let slot = self.contexts + idx * self.stride + self.pa_off;
        unsafe { write_u64(slot, addr as u64) };
    }
}

/// 定位 `ContextNode` 数组。两条结构化锚点，均不依赖指令编码或调试符号：
///
/// 1. bionic 在 contexts_serialized.cpp 里用 prctl(PR_SET_VMA_ANON_NAME) 给数组
///    命名为 "System property context nodes"，内核会在 /proc/self/maps 里显示。
/// 2. 若内核未启用 CONFIG_ANON_VMA_NAME，退化为「prop_area 指针锚定」：只在
///    可写映射里找值恰好等于某个真实 /dev/__properties__ 映射起点的槽位。
///
/// 两者都复用 validate_context_array 做完整结构校验。
fn locate_context_array(
    maps: &Maps,
    area_starts: &HashSet<usize>,
    probe: Option<usize>,
) -> anyhow::Result<ContextArray> {
    // 主路径：内核 VMA 名锚点。数组基址直接给出，只需推 stride / pa_off。
    for (base, end) in maps.named_context_arrays() {
        if let Some(array) = resolve_array_at_base(base, end - base, maps, area_starts, probe) {
            info!("prop backend: context array anchored by anon VMA name at {base:#x}");
            return Ok(array);
        }
    }

    // 退化路径：prop_area 指针锚定。全架构统一，不读任何机器码。
    let mut bases = Vec::new();
    for (start, end) in maps.writable_regions() {
        let mut cursor = start;
        while cursor + 8 <= end {
            if maps.readable(cursor, 8) {
                let value = unsafe { read_u64(cursor) } as usize;
                if area_starts.contains(&value) {
                    // 指针槽之前 8..=64 字节处可能是数组元素的 pa_off；把候选
                    // 基址集合作给 resolve，由结构校验裁定。
                    for back in (8..=64).step_by(8) {
                        if cursor >= back {
                            bases.push(cursor - back);
                        }
                    }
                }
            }
            cursor += 8;
        }
    }
    bases.sort_unstable();
    bases.dedup();
    for base in bases {
        if let Some(array) = resolve_array_at_base(base, 0, maps, area_starts, probe) {
            info!("prop backend: context array anchored by prop_area pointer at {base:#x}");
            return Ok(array);
        }
    }
    bail!("no self-consistent context array in named or prop_area-anchored mappings")
}

/// 已知数组基址，枚举 stride / pa_off 并做完整校验。
///
/// `span` 是 VMA 名锚点给出的精确字节数；退化锚点没有可靠 span，传 0 表示
/// 只按 stride 上限探测 count。
fn resolve_array_at_base(
    base: usize,
    span: usize,
    maps: &Maps,
    area_starts: &HashSet<usize>,
    probe: Option<usize>,
) -> Option<ContextArray> {
    if !maps.writable(base, 8) {
        return None;
    }
    for &stride in STRIDE_CANDIDATES {
        let count = if span > 0 {
            span / stride
        } else {
            // 退化路径：从映射剩余空间按 stride 推上限，交给 validate 收敛。
            let region_end = maps.region_end_from(base).unwrap_or(base + SCAN_WINDOW);
            (region_end - base) / stride
        };
        if !(8..=4096).contains(&count) || !maps.writable(base, count * stride) {
            continue;
        }
        for pa_off in (16..stride).step_by(8) {
            if validate_context_array(base, count, stride, pa_off, area_starts, probe, maps) {
                return Some(ContextArray {
                    base,
                    count,
                    stride,
                    pa_off,
                });
            }
        }
    }
    None
}

/// 调用 `__system_property_find` 探测某个 key 实际路由到哪个区。
fn probe_area_start(wrapper: usize, maps: &Maps) -> Option<usize> {
    type FindFn = unsafe extern "C" fn(*const libc::c_char) -> *const libc::c_void;
    let find: FindFn = unsafe { std::mem::transmute(wrapper) };
    for key in PROBE_KEYS {
        let Ok(ckey) = std::ffi::CString::new(*key) else {
            continue;
        };
        let pi = unsafe { find(ckey.as_ptr()) };
        if pi.is_null() {
            continue;
        }
        let addr = pi as usize;
        if let Some(r) = maps.region_of(addr)
            && r.path.starts_with(PROP_PREFIX)
            && r.offset == 0
        {
            return Some(r.start);
        }
    }
    None
}

struct ContextArray {
    base: usize,
    count: usize,
    stride: usize,
    pa_off: usize,
}

fn validate_context_array(
    base: usize,
    count: usize,
    stride: usize,
    pa_off: usize,
    area_starts: &HashSet<usize>,
    probe: Option<usize>,
    maps: &Maps,
) -> bool {
    let mut nonzero = 0usize;
    let mut distinct = HashSet::new();
    let mut has_probe = probe.is_none();
    for i in 0..count {
        let name = unsafe { read_u64(base + i * stride + pa_off - 8) } as usize;
        if !maps.context_name(name) {
            return false;
        }
        let v = unsafe { read_u64(base + i * stride + pa_off) } as usize;
        if v == 0 {
            continue;
        }
        if !area_starts.contains(&v) {
            return false;
        }
        nonzero += 1;
        distinct.insert(v);
        if Some(v) == probe {
            has_probe = true;
        }
    }
    nonzero >= 3 && distinct.len() >= 2 && has_probe
}
