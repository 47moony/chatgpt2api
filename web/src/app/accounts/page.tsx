"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { ComponentProps } from "react";
import {
  Ban,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  CircleOff,
  Copy,
  Download,
  KeyRound,
  LoaderCircle,
  Pencil,
  RefreshCw,
  Search,
  Trash2,
  UserRound,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  deleteAccounts,
  fetchAccounts,
  recoverAccounts,
  refreshAccounts,
  updateAccount,
  completeManualRegisteredAccountRecovery,
  type Account,
  type AccountStatus,
} from "@/lib/api";
import { useAuthGuard } from "@/lib/use-auth-guard";
import { cn } from "@/lib/utils";

import { AccountImportDialog } from "./components/account-import-dialog";

const accountStatusOptions: { label: string; value: AccountStatus | "all" }[] = [
  { label: "全部状态", value: "all" },
  { label: "正常", value: "正常" },
  { label: "限流", value: "限流" },
  { label: "异常", value: "异常" },
  { label: "禁用", value: "禁用" },
];

const statusMeta: Record<
  AccountStatus,
  {
    icon: typeof CheckCircle2;
    badge: ComponentProps<typeof Badge>["variant"];
  }
> = {
  正常: { icon: CheckCircle2, badge: "success" },
  限流: { icon: CircleAlert, badge: "warning" },
  异常: { icon: CircleOff, badge: "danger" },
  禁用: { icon: Ban, badge: "secondary" },
};

const metricCards = [
  { key: "total", label: "账户总数", color: "text-stone-900", icon: UserRound },
  { key: "active", label: "正常账户", color: "text-emerald-600", icon: CheckCircle2 },
  { key: "limited", label: "限流账户", color: "text-orange-500", icon: CircleAlert },
  { key: "abnormal", label: "异常账户", color: "text-rose-500", icon: CircleOff },
  { key: "disabled", label: "禁用账户", color: "text-stone-500", icon: Ban },
  { key: "quota", label: "剩余额度", color: "text-blue-500", icon: RefreshCw },
] as const;

function isUnlimitedImageQuotaAccount(account: Account) {
  return account.type === "pro" || account.type === "prolite";
}

function imageQuotaUnknown(account: Account) {
  return Boolean(account.image_quota_unknown);
}

function formatCompact(value: number) {
  if (value >= 1000) {
    return `${(value / 1000).toFixed(1)}k`;
  }
  return String(value);
}

function formatQuota(account: Account) {
  if (isUnlimitedImageQuotaAccount(account)) {
    return "∞";
  }
  if (imageQuotaUnknown(account)) {
    return "未知";
  }
  return String(Math.max(0, account.quota));
}

function formatRestoreAt(value?: string | null) {
  if (!value) {
    return { absolute: "—", relative: "" };
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return { absolute: value, relative: "" };
  }

  const diffMs = Math.max(0, date.getTime() - Date.now());
  const totalHours = Math.ceil(diffMs / (1000 * 60 * 60));
  const days = Math.floor(totalHours / 24);
  const hours = totalHours % 24;
  const relative = diffMs > 0 ? `剩余 ${days}d ${hours}h` : "已到恢复时间";

  const pad = (num: number) => String(num).padStart(2, "0");
  const absolute = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;

  return { absolute, relative };
}

function formatQuotaSummary(accounts: Account[]) {
  const availableAccounts = accounts.filter((account) => account.status === "正常");
  if (availableAccounts.some(isUnlimitedImageQuotaAccount)) {
    return "∞";
  }
  if (availableAccounts.some(imageQuotaUnknown)) {
    return "未知";
  }
  return formatCompact(availableAccounts.reduce((sum, account) => sum + Math.max(0, account.quota), 0));
}

function maskToken(token?: string) {
  if (!token) return "—";
  if (token.length <= 18) return token;
  return `${token.slice(0, 16)}...${token.slice(-8)}`;
}

function downloadTokens(accounts: Account[]) {
  const content = `${accounts.map((account) => account.access_token).join("\n")}\n`;
  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `accounts-${Date.now()}.txt`;
  link.click();
  URL.revokeObjectURL(url);
}

function displayAccountType(account: Account) {
  return account.type || "Free";
}

function displayProxy(account: Account) {
  const proxy = account.proxy;
  if (proxy?.name) return proxy.name;
  if (proxy?.host && proxy?.port) return `${proxy.host}:${proxy.port}`;
  return account.proxy_key || "—";
}

function sourceMeta(account: Account): {
  label: string;
  detail: string;
  badge: ComponentProps<typeof Badge>["variant"];
} {
  const owner = String(account.credential_owner || "").trim();
  if (owner === "sub2api" || account.sub2api_account_id) {
    return {
      label: "sub2api",
      detail: account.sub2api_account_id ? `ID ${account.sub2api_account_id}` : "refresh 由 sub2api 管理",
      badge: "violet",
    };
  }
  if (owner === "chatgpt2api" || account.register_job_id) {
    return {
      label: "注册机",
      detail: account.register_job_id ? `任务 ${account.register_job_id.slice(0, 8)}` : "chat 本地管理",
      badge: "info",
    };
  }
  if (owner) {
    return { label: owner, detail: "自定义来源", badge: "secondary" };
  }
  return { label: "手动 Token", detail: "无本地 refresh 管理", badge: "secondary" };
}

function accountEmail(account: Account) {
  return account.email || account.login?.email || "";
}

function isSub2APIOwnedAccount(account: Account) {
  return String(account.credential_owner || "").trim() === "sub2api" || Boolean(account.sub2api_account_id);
}

function isTerminalAccount(account: Account) {
  const text = String(account.last_error || "").toLowerCase();
  return text.includes("deleted or deactivated") || text.includes("远端删除") || text.includes("停用");
}

function isRecoverableAuthError(account: Account) {
  const text = String(account.last_error || "").toLowerCase();
  if (isTerminalAccount(account)) {
    return false;
  }
  const networkMarkers = [
    "tls",
    "ssl",
    "timed out",
    "timeout",
    "connection",
    "proxy",
    "temporarily unavailable",
    "remote end closed",
    "curl",
  ];
  if (networkMarkers.some((marker) => text.includes(marker))) {
    return false;
  }
  const authMarkers = [
    "401",
    "unauthorized",
    "token_invalidated",
    "authentication token has been invalidated",
    "invalid access token",
    "invalid_access_token",
    "refresh failed",
    "refresh token unavailable",
    "refresh_token_http_400",
    "refresh_token_http_401",
    "refresh_token_http_403",
    "refresh_token_missing_access_token",
    "refresh_token_missing_client_id",
  ];
  return authMarkers.some((marker) => text.includes(marker));
}

function canRecoverAccount(account: Account) {
  return (
    account.status === "异常" &&
    Boolean(accountEmail(account)) &&
    !isSub2APIOwnedAccount(account) &&
    isRecoverableAuthError(account)
  );
}

function isTerminalRecoverError(item: RecoverDialogState["errors"][number]) {
  const text = String(item.error || "").toLowerCase();
  return Boolean(item.terminal) || text.includes("deleted or deactivated") || text.includes("远端删除") || text.includes("停用");
}

function recoverDisableReason(account: Account) {
  if (account.status !== "异常") return "账号未标记异常";
  if (!accountEmail(account)) return "缺少邮箱";
  if (isSub2APIOwnedAccount(account)) return "sub2api 来源账号不由 chatgpt2api 恢复";
  if (isTerminalAccount(account)) return "远端已删除或停用";
  if (!isRecoverableAuthError(account)) return "当前异常不是认证失败或 refresh 失败";
  return "重新登录恢复凭据";
}

type RecoverDialogState = {
  open: boolean;
  status: "running" | "done" | "error";
  requested: number;
  recovered: number;
  errors: Array<{
    token?: string;
    delete_token?: string;
    email?: string;
    error: string;
    manual_code_required?: boolean;
    session_id?: string;
    api_base?: string;
    mailbox?: Record<string, unknown>;
    terminal?: boolean;
    terminal_action?: string;
  }>;
  message?: string;
};

type ManualRecoveryState = {
  sessionId: string;
  email?: string;
  apiBase?: string;
  code: string;
  deleteToken?: string;
};

function AccountsPageContent() {
  const didLoadRef = useRef(false);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [typeFilter, setTypeFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState<AccountStatus | "all">("all");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState("10");
  const [editingAccount, setEditingAccount] = useState<Account | null>(null);
  const [editStatus, setEditStatus] = useState<AccountStatus>("正常");
  const [isLoading, setIsLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [isRecovering, setIsRecovering] = useState(false);
  const [isDeleting, setIsDeleting] = useState(false);
  const [isUpdating, setIsUpdating] = useState(false);
  const [recoverDialog, setRecoverDialog] = useState<RecoverDialogState | null>(null);
  const [manualRecovery, setManualRecovery] = useState<ManualRecoveryState | null>(null);

  const loadAccounts = async (silent = false) => {
    if (!silent) {
      setIsLoading(true);
    }
    try {
      const data = await fetchAccounts();
      setAccounts(data.items);
      setSelectedIds((prev) => prev.filter((id) => data.items.some((item) => item.access_token === id)));
    } catch (error) {
      const message = error instanceof Error ? error.message : "加载账户失败";
      toast.error(message);
    } finally {
      if (!silent) {
        setIsLoading(false);
      }
    }
  };

  useEffect(() => {
    if (didLoadRef.current) {
      return;
    }
    didLoadRef.current = true;
    void loadAccounts();
  }, []);

  const filteredAccounts = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    return accounts.filter((account) => {
      const searchMatched =
        normalizedQuery.length === 0 || (account.email ?? "").toLowerCase().includes(normalizedQuery);
      const typeMatched = typeFilter === "all" || displayAccountType(account) === typeFilter;
      const statusMatched = statusFilter === "all" || account.status === statusFilter;
      return searchMatched && typeMatched && statusMatched;
    });
  }, [accounts, query, statusFilter, typeFilter]);

  const pageCount = Math.max(1, Math.ceil(filteredAccounts.length / Number(pageSize)));
  const safePage = Math.min(page, pageCount);
  const startIndex = (safePage - 1) * Number(pageSize);
  const currentRows = filteredAccounts.slice(startIndex, startIndex + Number(pageSize));
  const allCurrentSelected =
    currentRows.length > 0 && currentRows.every((row) => selectedIds.includes(row.access_token));

  const summary = useMemo(() => {
    const total = accounts.length;
    const active = accounts.filter((item) => item.status === "正常").length;
    const limited = accounts.filter((item) => item.status === "限流").length;
    const abnormal = accounts.filter((item) => item.status === "异常").length;
    const disabled = accounts.filter((item) => item.status === "禁用").length;
    const quota = formatQuotaSummary(accounts);

    return { total, active, limited, abnormal, disabled, quota };
  }, [accounts]);

  const accountTypeOptions = useMemo(
    () => [
      { label: "全部类型", value: "all" },
      ...Array.from(new Set(accounts.map(displayAccountType))).map((type) => ({ label: type, value: type })),
    ],
    [accounts],
  );

  const selectedTokens = useMemo(() => {
    const selectedSet = new Set(selectedIds);
    return accounts.filter((item) => selectedSet.has(item.access_token)).map((item) => item.access_token);
  }, [accounts, selectedIds]);

  const abnormalTokens = useMemo(() => {
    return accounts.filter((item) => item.status === "异常").map((item) => item.access_token);
  }, [accounts]);

  const recoverableAbnormalTokens = useMemo(() => {
    return accounts
      .filter(canRecoverAccount)
      .map((item) => item.access_token);
  }, [accounts]);

  const selectedRecoverableTokens = useMemo(() => {
    const recoverableSet = new Set(recoverableAbnormalTokens);
    return selectedTokens.filter((token) => recoverableSet.has(token));
  }, [recoverableAbnormalTokens, selectedTokens]);

  const paginationItems = useMemo(() => {
    const items: (number | "...")[] = [];
    const start = Math.max(1, safePage - 1);
    const end = Math.min(pageCount, safePage + 1);

    if (start > 1) items.push(1);
    if (start > 2) items.push("...");
    for (let current = start; current <= end; current += 1) items.push(current);
    if (end < pageCount - 1) items.push("...");
    if (end < pageCount) items.push(pageCount);

    return items;
  }, [pageCount, safePage]);

  const handleDeleteTokens = async (tokens: string[]) => {
    if (tokens.length === 0) {
      toast.error("请先选择要删除的账户");
      return;
    }

    setIsDeleting(true);
    try {
      const data = await deleteAccounts(tokens);
      setAccounts(data.items);
      setSelectedIds((prev) => prev.filter((id) => data.items.some((item) => item.access_token === id)));
      toast.success(`删除 ${data.removed ?? 0} 个账户`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "删除账户失败";
      toast.error(message);
    } finally {
      setIsDeleting(false);
    }
  };

  const handleDeleteRecoverError = async (item: RecoverDialogState["errors"][number]) => {
    const token = String(item.delete_token || "").trim();
    if (!token) {
      toast.error("缺少可删除的本地池 token");
      return;
    }

    setIsDeleting(true);
    try {
      const data = await deleteAccounts([token]);
      setAccounts(data.items);
      setSelectedIds((prev) => prev.filter((id) => data.items.some((account) => account.access_token === id)));
      setRecoverDialog((prev) => {
        if (!prev) {
          return prev;
        }
        return {
          ...prev,
          errors: prev.errors.filter((errorItem) => errorItem !== item),
        };
      });
      if (manualRecovery?.deleteToken === token) {
        setManualRecovery(null);
      }
      toast.success(`删除 ${data.removed ?? 0} 个账户`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "删除账户失败";
      toast.error(message);
    } finally {
      setIsDeleting(false);
    }
  };

  const handleRefreshAccounts = async (accessTokens: string[]) => {
    if (accessTokens.length === 0) {
      toast.error("没有需要刷新的账户");
      return;
    }

    setIsRefreshing(true);
    try {
      const data = await refreshAccounts(accessTokens);
      setAccounts(data.items);
      setSelectedIds((prev) => prev.filter((id) => data.items.some((item) => item.access_token === id)));
      const extra = [
        data.removed ? `删除失效 ${data.removed} 个` : "",
      ].filter(Boolean).join("，");
      if (data.errors.length > 0) {
        const firstError = data.errors[0]?.error;
        toast.error(
          `刷新成功 ${data.refreshed} 个${extra ? `，${extra}` : ""}，失败 ${data.errors.length} 个${firstError ? `，首个错误：${firstError}` : ""}`,
        );
      } else {
        toast.success(`刷新成功 ${data.refreshed} 个账户${extra ? `，${extra}` : ""}`);
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "刷新账户失败";
      toast.error(message);
    } finally {
      setIsRefreshing(false);
    }
  };

  const handleRecoverAccounts = async (accessTokens: string[]) => {
    const recoverableSet = new Set(accounts.filter(canRecoverAccount).map((item) => item.access_token));
    const targetTokens = accessTokens.filter((token) => recoverableSet.has(token));
    if (targetTokens.length === 0) {
      toast.error("没有可恢复的账户");
      return;
    }

    setIsRecovering(true);
    setRecoverDialog({
      open: true,
      status: "running",
      requested: targetTokens.length,
      recovered: 0,
      errors: [],
    });
    try {
      const data = await recoverAccounts(targetTokens);
      setAccounts(data.items);
      setSelectedIds((prev) => prev.filter((id) => data.items.some((item) => item.access_token === id)));
      setRecoverDialog({
        open: true,
        status: "done",
        requested: targetTokens.length,
        recovered: data.recovered,
        errors: data.errors,
      });
      const manualError = data.errors.find((item) => item.manual_code_required && item.session_id);
      if (manualError?.session_id) {
        setManualRecovery({
          sessionId: manualError.session_id,
          email: manualError.email,
          apiBase: manualError.api_base,
          code: "",
          deleteToken: manualError.delete_token,
        });
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "恢复账户失败";
      setRecoverDialog({
        open: true,
        status: "error",
        requested: targetTokens.length,
        recovered: 0,
        errors: [],
        message,
      });
    } finally {
      setIsRecovering(false);
    }
  };

  const handleCompleteManualRecovery = async () => {
    if (!manualRecovery) {
      return;
    }

    setIsRecovering(true);
    try {
      const result = await completeManualRegisteredAccountRecovery(manualRecovery.sessionId, manualRecovery.code);
      if (result.status === "complete") {
        setManualRecovery(null);
        await loadAccounts(true);
        setRecoverDialog((prev) => prev ? { ...prev, recovered: prev.recovered + Number(result.recovered || 1) } : prev);
        toast.success("验证码恢复完成");
        return;
      }
      await loadAccounts(true);
      const errorText = result.error || "手动验证码恢复失败";
      const terminal = isTerminalRecoverError({ error: errorText });
      setRecoverDialog((prev) => {
        const errorItem = {
          email: result.email || manualRecovery.email,
          error: errorText,
          terminal,
          delete_token: terminal ? manualRecovery.deleteToken : undefined,
          terminal_action: terminal ? "delete_local_pool" : undefined,
        };
        if (!prev) {
          return {
            open: true,
            status: "done",
            requested: 1,
            recovered: 0,
            errors: [errorItem],
          };
        }
        return { ...prev, errors: [...prev.errors, errorItem] };
      });
      toast.error(errorText);
    } catch (error) {
      const message = error instanceof Error ? error.message : "手动验证码恢复失败";
      toast.error(message);
    } finally {
      setIsRecovering(false);
    }
  };

  const openEditDialog = (account: Account) => {
    setEditingAccount(account);
    setEditStatus(account.status);
  };

  const handleUpdateAccount = async () => {
    if (!editingAccount) {
      return;
    }

    setIsUpdating(true);
    try {
      const data = await updateAccount(editingAccount.access_token, {
        status: editStatus,
      });
      setAccounts(data.items);
      setSelectedIds((prev) => prev.filter((id) => data.items.some((item) => item.access_token === id)));
      setEditingAccount(null);
      toast.success("账号信息已更新");
    } catch (error) {
      const message = error instanceof Error ? error.message : "更新账号失败";
      toast.error(message);
    } finally {
      setIsUpdating(false);
    }
  };

  const toggleSelectAll = (checked: boolean) => {
    if (checked) {
      setSelectedIds((prev) => Array.from(new Set([...prev, ...currentRows.map((item) => item.access_token)])));
      return;
    }
    setSelectedIds((prev) => prev.filter((id) => !currentRows.some((row) => row.access_token === id)));
  };

  return (
    <>
      <section className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="space-y-1">
          <div className="text-xs font-semibold tracking-[0.18em] text-stone-500 uppercase">
            Account Pool
          </div>
          <h1 className="text-2xl font-semibold tracking-tight">号池管理</h1>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button
            variant="outline"
            className="h-10 rounded-xl border-stone-200 bg-white/80 px-4 text-stone-700 hover:bg-white"
            onClick={() => void loadAccounts()}
            disabled={isLoading || isRefreshing || isDeleting}
          >
            <RefreshCw className={cn("size-4", isLoading ? "animate-spin" : "")} />
            刷新列表
          </Button>
          <Button
            variant="outline"
            className="h-10 rounded-xl border-stone-200 bg-white/80 px-4 text-stone-700 hover:bg-white"
            onClick={() => void handleRefreshAccounts(accounts.map((item) => item.access_token))}
            disabled={isLoading || isRefreshing || isDeleting || accounts.length === 0}
          >
            <RefreshCw className={cn("size-4", isRefreshing ? "animate-spin" : "")} />
            一键刷新所有账号信息和额度
          </Button>
          <AccountImportDialog
            disabled={isLoading || isRefreshing || isDeleting}
            onImported={(items) => {
              setAccounts(items);
              setSelectedIds([]);
              setPage(1);
            }}
          />
          <Button
            variant="outline"
            className="h-10 rounded-xl border-stone-200 bg-white/80 px-4 text-stone-700 hover:bg-white"
            onClick={() => downloadTokens(accounts)}
            disabled={accounts.length === 0}
          >
            <Download className="size-4" />
            导出全部 Token
          </Button>
        </div>
      </section>

      <Dialog open={Boolean(editingAccount)} onOpenChange={(open) => (!open ? setEditingAccount(null) : null)}>
        <DialogContent showCloseButton={false} className="rounded-2xl p-6">
          <DialogHeader className="gap-2">
            <DialogTitle>编辑账户</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              手动修改账号状态。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="space-y-2">
              <label className="text-sm font-medium text-stone-700">状态</label>
              <Select value={editStatus} onValueChange={(value) => setEditStatus(value as AccountStatus)}>
                <SelectTrigger className="h-11 rounded-xl border-stone-200 bg-white">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {accountStatusOptions
                    .filter((option) => option.value !== "all")
                    .map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                </SelectContent>
              </Select>
            </div>
          </div>
          <DialogFooter className="pt-2">
            <Button
              variant="secondary"
              className="h-10 rounded-xl bg-stone-100 px-5 text-stone-700 hover:bg-stone-200"
              onClick={() => setEditingAccount(null)}
              disabled={isUpdating}
            >
              取消
            </Button>
            <Button
              className="h-10 rounded-xl bg-stone-950 px-5 text-white hover:bg-stone-800"
              onClick={() => void handleUpdateAccount()}
              disabled={isUpdating}
            >
              {isUpdating ? <LoaderCircle className="size-4 animate-spin" /> : null}
              保存修改
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={Boolean(recoverDialog?.open)}
        onOpenChange={(open) => {
          if (isRecovering) {
            return;
          }
          setRecoverDialog((prev) => (prev ? { ...prev, open } : prev));
        }}
      >
        <DialogContent showCloseButton={!isRecovering} className="rounded-2xl p-6 sm:max-w-[560px]">
          <DialogHeader className="gap-2">
            <DialogTitle>恢复账号凭据</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              重新登录只会处理已标记异常且能匹配注册记录的账号。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="grid grid-cols-3 gap-3">
              <div className="rounded-xl border border-stone-100 bg-stone-50 px-3 py-2">
                <div className="text-xs text-stone-500">本次处理</div>
                <div className="mt-1 text-lg font-semibold text-stone-900">
                  {recoverDialog?.requested ?? 0}
                </div>
              </div>
              <div className="rounded-xl border border-emerald-100 bg-emerald-50 px-3 py-2">
                <div className="text-xs text-emerald-700">恢复成功</div>
                <div className="mt-1 text-lg font-semibold text-emerald-700">
                  {recoverDialog?.recovered ?? 0}
                </div>
              </div>
              <div className="rounded-xl border border-rose-100 bg-rose-50 px-3 py-2">
                <div className="text-xs text-rose-700">恢复失败</div>
                <div className="mt-1 text-lg font-semibold text-rose-700">
                  {recoverDialog?.errors.length ?? 0}
                </div>
              </div>
            </div>

            {recoverDialog?.status === "running" ? (
              <div className="flex items-center gap-3 rounded-xl border border-blue-100 bg-blue-50 px-4 py-3 text-sm text-blue-700">
                <LoaderCircle className="size-4 animate-spin" />
                正在重新登录并换取凭据，完成后会保留结果明细。
              </div>
            ) : null}

            {recoverDialog?.status === "error" ? (
              <div className="rounded-xl border border-rose-100 bg-rose-50 px-4 py-3 text-sm leading-6 text-rose-700">
                {recoverDialog.message || "恢复账户失败"}
              </div>
            ) : null}

            {recoverDialog?.status === "done" && recoverDialog.errors.length === 0 ? (
              <div className="rounded-xl border border-emerald-100 bg-emerald-50 px-4 py-3 text-sm leading-6 text-emerald-700">
                本次恢复全部成功，账号列表已同步最新凭据。
              </div>
            ) : null}

            {recoverDialog?.errors.length ? (
              <div className="space-y-2">
                <div className="text-sm font-medium text-stone-700">失败明细</div>
                <div className="max-h-56 space-y-2 overflow-auto rounded-xl border border-stone-100 bg-stone-50 p-2">
                  {recoverDialog.errors.map((item, index) => {
                    const terminal = isTerminalRecoverError(item);
                    return (
                      <div key={`${item.email || item.token || index}`} className="rounded-lg bg-white px-3 py-2 text-sm leading-6">
                        <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                          <div className="min-w-0">
                            <div className="font-medium text-stone-700">
                              {item.email || maskToken(item.token) || `账号 ${index + 1}`}
                            </div>
                            <div className="break-words text-rose-600">{item.error}</div>
                            {terminal ? (
                              <div className="mt-1 text-xs text-stone-500">
                                远端已返回删除或停用，不能通过验证码恢复。
                              </div>
                            ) : null}
                          </div>
                          {terminal ? (
                            <Button
                              variant="outline"
                              size="sm"
                              className="h-8 shrink-0 rounded-lg border-rose-200 bg-white px-3 text-rose-600 hover:bg-rose-50 hover:text-rose-700"
                              onClick={() => void handleDeleteRecoverError(item)}
                              disabled={isDeleting || !item.delete_token}
                            >
                              {isDeleting ? <LoaderCircle className="size-3.5 animate-spin" /> : <Trash2 className="size-3.5" />}
                              删除本地池
                            </Button>
                          ) : null}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            ) : null}
          </div>
          <DialogFooter className="pt-2">
            <Button
              className="h-10 rounded-xl bg-stone-950 px-5 text-white hover:bg-stone-800"
              onClick={() => setRecoverDialog((prev) => (prev ? { ...prev, open: false } : prev))}
              disabled={isRecovering}
            >
              关闭
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={Boolean(manualRecovery)} onOpenChange={(open) => (!open ? setManualRecovery(null) : null)}>
        <DialogContent showCloseButton={false} className="rounded-2xl p-6 sm:max-w-[460px]">
          <DialogHeader className="gap-2">
            <DialogTitle>手动验证码恢复</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              已触发 {manualRecovery?.email || "该账号"} 的登录验证码，输入邮箱收到的 6 位验证码后继续换取凭据。
            </DialogDescription>
          </DialogHeader>
          <div className="rounded-xl border border-stone-100 bg-stone-50 px-3 py-2 text-sm leading-6 text-stone-600">
            API Base：<span className="font-mono">{manualRecovery?.apiBase || "-"}</span>
          </div>
          <div className="space-y-2">
            <label className="text-sm font-medium text-stone-700">邮箱验证码</label>
            <Input
              value={manualRecovery?.code || ""}
              onChange={(event) => {
                const value = event.target.value.replace(/\D/g, "").slice(0, 6);
                setManualRecovery((prev) => (prev ? { ...prev, code: value } : prev));
              }}
              placeholder="6 位验证码"
              className="h-11 rounded-xl border-stone-200 bg-white font-mono text-lg tracking-[0.3em]"
            />
          </div>
          <DialogFooter className="pt-2">
            <Button
              type="button"
              variant="secondary"
              className="h-10 rounded-xl bg-stone-100 px-5 text-stone-700 hover:bg-stone-200"
              onClick={() => setManualRecovery(null)}
              disabled={isRecovering}
            >
              取消
            </Button>
            <Button
              type="button"
              className="h-10 rounded-xl bg-stone-950 px-5 text-white hover:bg-stone-800"
              onClick={() => void handleCompleteManualRecovery()}
              disabled={isRecovering || (manualRecovery?.code.length || 0) !== 6}
            >
              {isRecovering ? <LoaderCircle className="size-4 animate-spin" /> : null}
              提交验证码
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <section className="space-y-3">
        <div className="grid gap-3 md:grid-cols-3 xl:grid-cols-6">
          {metricCards.map((item) => {
            const Icon = item.icon;
            const value = summary[item.key];
            return (
              <Card key={item.key} className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
                <CardContent className="p-4">
                  <div className="mb-4 flex items-start justify-between">
                    <span className="text-xs font-medium text-stone-400">{item.label}</span>
                    <Icon className="size-4 text-stone-400" />
                  </div>
                  <div className={cn("text-[1.75rem] font-semibold tracking-tight", item.color)}>
                    <span className={typeof value === "number" ? "" : "text-[1.1rem]"}>
                      {typeof value === "number" ? formatCompact(value) : value}
                    </span>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      </section>

      <section className="space-y-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex items-center gap-3">
            <h2 className="text-lg font-semibold tracking-tight">账户列表</h2>
            <Badge variant="secondary" className="rounded-lg bg-stone-200 px-2 py-0.5 text-stone-700">
              {filteredAccounts.length}
            </Badge>
          </div>

          <div className="flex flex-col gap-2 lg:flex-row lg:items-center">
            <div className="relative min-w-[260px]">
              <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-stone-400" />
              <Input
                value={query}
                onChange={(event) => {
                  setQuery(event.target.value);
                  setPage(1);
                }}
                placeholder="搜索邮箱"
                className="h-10 rounded-xl border-stone-200 bg-white/85 pl-10"
              />
            </div>
            <Select
              value={typeFilter}
              onValueChange={(value) => {
                setTypeFilter(value);
                setPage(1);
              }}
            >
              <SelectTrigger className="h-10 w-full rounded-xl border-stone-200 bg-white/85 lg:w-[150px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {accountTypeOptions.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Select
              value={statusFilter}
              onValueChange={(value) => {
                setStatusFilter(value as AccountStatus | "all");
                setPage(1);
              }}
            >
              <SelectTrigger className="h-10 w-full rounded-xl border-stone-200 bg-white/85 lg:w-[150px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {accountStatusOptions.map((option) => (
                  <SelectItem key={option.value} value={option.value}>
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        {isLoading && accounts.length === 0 ? (
          <Card className="rounded-2xl border-white/80 bg-white/90 shadow-sm">
            <CardContent className="flex flex-col items-center justify-center gap-3 px-6 py-14 text-center">
              <div className="rounded-xl bg-stone-100 p-3 text-stone-500">
                <LoaderCircle className="size-5 animate-spin" />
              </div>
              <div className="space-y-1">
                <p className="text-sm font-medium text-stone-700">正在加载账户</p>
                <p className="text-sm text-stone-500">从后端同步账号列表和状态。</p>
              </div>
            </CardContent>
          </Card>
        ) : null}

        <Card
          className={cn(
            "overflow-hidden rounded-2xl border-white/80 bg-white/90 shadow-sm",
            isLoading && accounts.length === 0 ? "hidden" : "",
          )}
        >
          <CardContent className="space-y-0 p-0">
            <div className="flex flex-col gap-3 border-b border-stone-100 px-4 py-3 lg:flex-row lg:items-center lg:justify-between">
              <div className="flex flex-wrap items-center gap-2 text-sm text-stone-500">
                <Button
                  variant="ghost"
                  className="h-8 rounded-lg px-3 text-stone-500 hover:bg-stone-100"
                  onClick={() => void handleRefreshAccounts(selectedTokens)}
                  disabled={selectedTokens.length === 0 || isRefreshing || isRecovering}
                >
                  {isRefreshing ? <LoaderCircle className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
                  刷新选中账号信息和额度
                </Button>
                <Button
                  variant="ghost"
                  className="h-8 rounded-lg px-3 text-blue-600 hover:bg-blue-50 hover:text-blue-700"
                  onClick={() => void handleRecoverAccounts(selectedRecoverableTokens)}
                  disabled={selectedRecoverableTokens.length === 0 || isRecovering}
                >
                  {isRecovering ? <LoaderCircle className="size-4 animate-spin" /> : <KeyRound className="size-4" />}
                  恢复选中异常账号
                </Button>
                <Button
                  variant="ghost"
                  className="h-8 rounded-lg px-3 text-rose-500 hover:bg-rose-50 hover:text-rose-600"
                  onClick={() => void handleDeleteTokens(abnormalTokens)}
                  disabled={abnormalTokens.length === 0 || isDeleting}
                >
                  {isDeleting ? <LoaderCircle className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
                  移除异常账号
                </Button>
                <Button
                  variant="ghost"
                  className="h-8 rounded-lg px-3 text-rose-500 hover:bg-rose-50 hover:text-rose-600"
                  onClick={() => void handleDeleteTokens(selectedTokens)}
                  disabled={selectedTokens.length === 0 || isDeleting}
                >
                  {isDeleting ? <LoaderCircle className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
                  删除所选
                </Button>
                {selectedIds.length > 0 ? (
                  <span className="rounded-lg bg-stone-100 px-2.5 py-1 text-xs font-medium text-stone-600">
                    已选择 {selectedIds.length} 项
                  </span>
                ) : null}
              </div>
            </div>

            <div className="overflow-x-auto">
              <table className="w-full min-w-[1240px] text-left">
                <thead className="border-b border-stone-100 text-[11px] text-stone-400 uppercase tracking-[0.18em]">
                  <tr>
                    <th className="w-12 px-4 py-3">
                      <Checkbox
                        checked={allCurrentSelected}
                        onCheckedChange={(checked) => toggleSelectAll(Boolean(checked))}
                      />
                    </th>
                    <th className="w-56 px-4 py-3">token</th>
                    <th className="w-28 px-4 py-3">类型</th>
                    <th className="w-24 px-4 py-3">状态</th>
                    <th className="w-36 px-4 py-3">来源</th>
                    <th className="w-64 px-4 py-3">账号信息</th>
                    <th className="w-44 px-4 py-3">当前代理</th>
                    <th className="w-24 px-4 py-3">额度</th>
                    <th className="w-40 px-4 py-3">恢复时间</th>
                    <th className="w-18 px-4 py-3">成功</th>
                    <th className="w-18 px-4 py-3">失败</th>
                    <th className="w-24 px-4 py-3">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {currentRows.map((account) => {
                    const terminalAccount = isTerminalAccount(account);
                    const status = terminalAccount ? statusMeta["异常"] : statusMeta[account.status];
                    const StatusIcon = status.icon;
                    const source = sourceMeta(account);

                    return (
                      <tr
                        key={account.access_token}
                        className="border-b border-stone-100/80 text-sm text-stone-600 transition-colors hover:bg-stone-50/70"
                      >
                        <td className="px-4 py-3">
                          <Checkbox
                            checked={selectedIds.includes(account.access_token)}
                            onCheckedChange={(checked) => {
                              setSelectedIds((prev) =>
                                checked
                                  ? Array.from(new Set([...prev, account.access_token]))
                                  : prev.filter((item) => item !== account.access_token),
                              );
                            }}
                          />
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-2">
                            <span className="font-medium tracking-tight text-stone-700">
                              {maskToken(account.access_token)}
                            </span>
                            <button
                              type="button"
                              className="rounded-lg p-1 text-stone-400 transition hover:bg-stone-100 hover:text-stone-700"
                              onClick={() => {
                                void navigator.clipboard.writeText(account.access_token);
                                toast.success("token 已复制");
                              }}
                            >
                              <Copy className="size-4" />
                            </button>
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          <Badge variant="secondary" className="rounded-md bg-stone-100 text-stone-700">
                            {displayAccountType(account)}
                          </Badge>
                        </td>
                        <td className="px-4 py-3">
                          <Badge
                            variant={status.badge}
                            className="inline-flex items-center gap-1 rounded-md px-2 py-1"
                          >
                            <StatusIcon className="size-3.5" />
                            {terminalAccount ? "远端停用" : account.status}
                          </Badge>
                          {terminalAccount ? (
                            <div className="mt-1 max-w-[120px] truncate text-xs text-rose-500" title={account.last_error || ""}>
                              不可恢复
                            </div>
                          ) : null}
                        </td>
                        <td className="px-4 py-3">
                          <div className="space-y-1">
                            <Badge variant={source.badge} className="rounded-md">
                              {source.label}
                            </Badge>
                            <div className="max-w-[150px] truncate text-xs leading-5 text-stone-500" title={source.detail}>
                              {source.detail}
                            </div>
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          {(() => {
                            const loginEmail = account.login?.email || account.email || "";
                            return (
                              <div className="space-y-1 text-xs leading-5 text-stone-500">
                                <div className="flex items-center gap-1">
                                  <span>{loginEmail || "—"}</span>
                                  {loginEmail ? (
                                    <button
                                      type="button"
                                      className="rounded p-1 text-stone-400 transition hover:bg-stone-100 hover:text-stone-700"
                                      onClick={() => {
                                        void navigator.clipboard.writeText(loginEmail);
                                        toast.success("邮箱已复制");
                                      }}
                                    >
                                      <Copy className="size-3.5" />
                                    </button>
                                  ) : null}
                                </div>
                              </div>
                            );
                          })()}
                        </td>
                        <td className="px-4 py-3 text-xs leading-5 text-stone-500">
                          {displayProxy(account)}
                        </td>
                        <td className="px-4 py-3">
                          <Badge variant="info" className="rounded-md">
                            {formatQuota(account)}
                          </Badge>
                        </td>
                        <td className="px-4 py-3 text-xs leading-5 text-stone-500">
                          {(() => {
                            const restore = formatRestoreAt(account.restore_at);
                            return (
                              <div className="space-y-0.5">
                                {restore.relative ? <div className="font-medium text-stone-700">{restore.relative}</div> : null}
                                <div>{restore.absolute}</div>
                              </div>
                            );
                          })()}
                        </td>
                        <td className="px-4 py-3 text-stone-500">{account.success}</td>
                        <td className="px-4 py-3 text-stone-500">{account.fail}</td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-1 text-stone-400">
                            <button
                              type="button"
                              className="rounded-lg p-2 transition hover:bg-stone-100 hover:text-stone-700"
                              onClick={() => openEditDialog(account)}
                              disabled={isUpdating}
                            >
                              <Pencil className="size-4" />
                            </button>
                            <button
                              type="button"
                              className="rounded-lg p-2 transition hover:bg-stone-100 hover:text-stone-700"
                              onClick={() => void handleRefreshAccounts([account.access_token])}
                              disabled={isRefreshing || isRecovering}
                              title="刷新账号信息和额度"
                            >
                              <RefreshCw className={cn("size-4", isRefreshing ? "animate-spin" : "")} />
                            </button>
                            <button
                              type="button"
                              className="rounded-lg p-2 transition hover:bg-blue-50 hover:text-blue-600 disabled:cursor-not-allowed disabled:opacity-40"
                              onClick={() => void handleRecoverAccounts([account.access_token])}
                              disabled={isRecovering || !canRecoverAccount(account)}
                              title={recoverDisableReason(account)}
                            >
                              {isRecovering ? <LoaderCircle className="size-4 animate-spin" /> : <KeyRound className="size-4" />}
                            </button>
                            <button
                              type="button"
                              className="rounded-lg p-2 transition hover:bg-rose-50 hover:text-rose-500"
                              onClick={() => void handleDeleteTokens([account.access_token])}
                              disabled={isDeleting}
                            >
                              <Trash2 className="size-4" />
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>

              {!isLoading && currentRows.length === 0 ? (
                <div className="flex flex-col items-center justify-center gap-3 px-6 py-14 text-center">
                  <div className="rounded-xl bg-stone-100 p-3 text-stone-500">
                    <Search className="size-5" />
                  </div>
                  <div className="space-y-1">
                    <p className="text-sm font-medium text-stone-700">没有匹配的账户</p>
                    <p className="text-sm text-stone-500">调整筛选条件或搜索关键字后重试。</p>
                  </div>
                </div>
              ) : null}
            </div>

            <div className="border-t border-stone-100 px-4 py-4">
              <div className="flex items-center justify-center gap-3 overflow-x-auto whitespace-nowrap">
                <div className="shrink-0 text-sm text-stone-500">
                显示第 {filteredAccounts.length === 0 ? 0 : startIndex + 1} -{" "}
                {Math.min(startIndex + Number(pageSize), filteredAccounts.length)} 条，共{" "}
                {filteredAccounts.length} 条
                </div>

                <span className="shrink-0 text-sm leading-none text-stone-500">
                  {safePage} / {pageCount} 页
                </span>
                <Select
                  value={pageSize}
                  onValueChange={(value) => {
                    setPageSize(value);
                    setPage(1);
                  }}
                >
                  <SelectTrigger className="h-10 w-[108px] shrink-0 rounded-lg border-stone-200 bg-white text-sm leading-none">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="10">10 / 页</SelectItem>
                    <SelectItem value="20">20 / 页</SelectItem>
                    <SelectItem value="50">50 / 页</SelectItem>
                    <SelectItem value="100">100 / 页</SelectItem>
                  </SelectContent>
                </Select>
                <Button
                  variant="outline"
                  size="icon"
                  className="size-10 shrink-0 rounded-lg border-stone-200 bg-white"
                  disabled={safePage <= 1}
                  onClick={() => setPage((prev) => Math.max(1, prev - 1))}
                >
                  <ChevronLeft className="size-4" />
                </Button>
                {paginationItems.map((item, index) =>
                  item === "..." ? (
                    <span key={`ellipsis-${index}`} className="px-1 text-sm text-stone-400">
                      ...
                    </span>
                  ) : (
                    <Button
                      key={item}
                      variant={item === safePage ? "default" : "outline"}
                      className={cn(
                        "h-10 min-w-10 shrink-0 rounded-lg px-3",
                        item === safePage
                          ? "bg-stone-950 text-white hover:bg-stone-800"
                          : "border-stone-200 bg-white text-stone-700",
                      )}
                      onClick={() => setPage(item)}
                    >
                      {item}
                    </Button>
                  ),
                )}
                <Button
                  variant="outline"
                  size="icon"
                  className="size-10 shrink-0 rounded-lg border-stone-200 bg-white"
                  disabled={safePage >= pageCount}
                  onClick={() => setPage((prev) => Math.min(pageCount, prev + 1))}
                >
                  <ChevronRight className="size-4" />
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      </section>
    </>
  );
}

export default function AccountsPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);

  if (isCheckingAuth || !session || session.role !== "admin") {
    return (
      <div className="flex min-h-[40vh] items-center justify-center">
        <LoaderCircle className="size-5 animate-spin text-stone-400" />
      </div>
    );
  }

  return <AccountsPageContent />;
}
