"use client";

import { AlertTriangle, Copy, Download, Eye, EyeOff, LoaderCircle, Maximize2, Plus, Play, RotateCcw, Save, Search, Square, Trash2, UserCheck, UserPlus } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import {
  checkRegisterSub2APIExport,
  downloadSub2APIExport,
  getSub2APIRegisterExportUrl,
  type RegisterSub2APICheck,
} from "@/lib/api";

import { useSettingsStore } from "../../settings/store";

export function RegisterCard() {
  const [showRegisteredPasswords, setShowRegisteredPasswords] = useState(false);
  const [selectedRegisteredEmails, setSelectedRegisteredEmails] = useState<string[]>([]);
  const [registeredEmailQuery, setRegisteredEmailQuery] = useState("");
  const [registeredAccountsOpen, setRegisteredAccountsOpen] = useState(false);
  const [isCheckingExport, setIsCheckingExport] = useState(false);
  const [exportCheck, setExportCheck] = useState<RegisterSub2APICheck | null>(null);
  const [manualRecovery, setManualRecovery] = useState<{ sessionId: string; email: string; apiBase?: string; code: string } | null>(null);
  const config = useSettingsStore((state) => state.registerConfig);
  const isLoading = useSettingsStore((state) => state.isLoadingRegister);
  const isSaving = useSettingsStore((state) => state.isSavingRegister);
  const setProxy = useSettingsStore((state) => state.setRegisterProxy);
  const setTotal = useSettingsStore((state) => state.setRegisterTotal);
  const setThreads = useSettingsStore((state) => state.setRegisterThreads);
  const setMode = useSettingsStore((state) => state.setRegisterMode);
  const setTargetQuota = useSettingsStore((state) => state.setRegisterTargetQuota);
  const setTargetAvailable = useSettingsStore((state) => state.setRegisterTargetAvailable);
  const setCheckInterval = useSettingsStore((state) => state.setRegisterCheckInterval);
  const setAddToLocalPool = useSettingsStore((state) => state.setRegisterAddToLocalPool);
  const setMailField = useSettingsStore((state) => state.setRegisterMailField);
  const addProvider = useSettingsStore((state) => state.addRegisterProvider);
  const updateProvider = useSettingsStore((state) => state.updateRegisterProvider);
  const deleteProvider = useSettingsStore((state) => state.deleteRegisterProvider);
  const deleteRegisteredAccounts = useSettingsStore((state) => state.deleteRegisteredAccounts);
  const backfillRegisteredMailMetadata = useSettingsStore((state) => state.backfillRegisteredMailMetadata);
  const recoverRegisteredAccounts = useSettingsStore((state) => state.recoverRegisteredAccounts);
  const completeManualRegisteredAccountRecovery = useSettingsStore((state) => state.completeManualRegisteredAccountRecovery);
  const importRegisteredAccountsToLocalPool = useSettingsStore((state) => state.importRegisteredAccountsToLocalPool);
  const save = useSettingsStore((state) => state.saveRegister);
  const toggle = useSettingsStore((state) => state.toggleRegister);
  const reset = useSettingsStore((state) => state.resetRegister);

  if (isLoading) {
    return (
      <div className="flex items-center justify-center rounded-xl border border-stone-200 bg-white/80 p-10">
        <LoaderCircle className="size-5 animate-spin text-stone-400" />
      </div>
    );
  }

  if (!config) return null;

  const stats = config.stats || { success: 0, fail: 0, done: 0, running: 0, threads: config.threads };
  const addToLocalPool = Boolean(config.add_to_local_pool);
  const providers = config.mail.providers || [];
  const logs = config.logs || [];
  const registeredAccounts = config.registered_accounts || [];
  const currentJobId = String(stats.job_id || "");
  const currentRegisteredAccounts = currentJobId
    ? registeredAccounts.filter((account) => String(account.job_id || "") === currentJobId)
    : registeredAccounts;
  const normalizedRegisteredEmailQuery = registeredEmailQuery.trim().toLowerCase();
  const scopedRegisteredAccounts = currentRegisteredAccounts.length ? currentRegisteredAccounts : registeredAccounts;
  const visibleRegisteredAccounts = scopedRegisteredAccounts
    .filter((account) => {
      if (!normalizedRegisteredEmailQuery) return true;
      return String(account.email || "").toLowerCase().includes(normalizedRegisteredEmailQuery);
    })
    .slice()
    .reverse();
  const visibleEmails = visibleRegisteredAccounts.map((account) => account.email);
  const selectedVisibleEmails = selectedRegisteredEmails.filter((email) => visibleEmails.includes(email));
  const importableVisibleEmails = visibleRegisteredAccounts
    .filter((account) => !account.imported_to_local_pool && Boolean(String((account as { access_token?: string }).access_token || "").trim()))
    .map((account) => account.email);
  const importableSelectedEmails = visibleRegisteredAccounts
    .filter((account) => selectedRegisteredEmails.includes(account.email))
    .filter((account) => !account.imported_to_local_pool && Boolean(String((account as { access_token?: string }).access_token || "").trim()))
    .map((account) => account.email);
  const checkByEmail = new Map((exportCheck?.items || []).map((item) => [item.email.toLowerCase(), item]));
  const accountCredentialIssue = (account: (typeof visibleRegisteredAccounts)[number]) => {
    const oauth = (account as { oauth?: Record<string, unknown> }).oauth || {};
    return Boolean(
      account.auth_failed
      || !String((account as { access_token?: string }).access_token || "").trim()
      || !String((account as { refresh_token?: string }).refresh_token || oauth.refresh_token || "").trim()
      || !String((account as { id_token?: string }).id_token || oauth.id_token || "").trim()
    );
  };
  const accountMetadataIssue = (account: (typeof visibleRegisteredAccounts)[number]) => {
    const oauth = (account as { oauth?: Record<string, unknown> }).oauth || {};
    return Boolean(
      !accountCredentialIssue(account)
      && !String(oauth.chatgpt_account_id || "").trim()
    );
  };
  const accountNeedsRecovery = (account: (typeof visibleRegisteredAccounts)[number]) => {
    const check = checkByEmail.get(String(account.email || "").toLowerCase());
    if (accountCredentialIssue(account)) return true;
    if (!check || check.exportable) return false;
    const blockingErrors = check.errors.filter((error) => !error.includes("chatgpt_account_id"));
    return blockingErrors.length > 0;
  };
  const recoverableVisibleEmails = visibleRegisteredAccounts
    .filter(accountNeedsRecovery)
    .map((account) => account.email);
  const recoverableSelectedEmails = visibleRegisteredAccounts
    .filter((account) => selectedRegisteredEmails.includes(account.email))
    .filter(accountNeedsRecovery)
    .map((account) => account.email);
  const allVisibleSelected =
    visibleRegisteredAccounts.length > 0 && visibleRegisteredAccounts.every((account) => selectedRegisteredEmails.includes(account.email));
  const runExportDownload = async (proxy: boolean, emails: string[]) => {
    await downloadSub2APIExport(
      getSub2APIRegisterExportUrl(proxy, emails),
      `sub2api-register-${proxy ? "with-proxy" : "no-proxy"}.json`,
    );
    const scopeText = emails.length > 0 ? `${emails.length} 个账号` : "全部已注册账号";
    toast.success(proxy ? `已导出 ${scopeText} sub2api JSON（带代理）` : `已导出 ${scopeText} sub2api JSON`);
  };
  const exportSub2API = async (proxy: boolean) => {
    const requestedEmails = selectedVisibleEmails.length > 0 ? selectedVisibleEmails : visibleEmails;
    setIsCheckingExport(true);
    setExportCheck(null);
    try {
      const data = await checkRegisterSub2APIExport(requestedEmails, true);
      const check = data.check;
      const exportableEmails = check.items.filter((item) => item.exportable).map((item) => item.email);
      if (exportableEmails.length === 0) {
        setExportCheck(check);
        toast.error(`没有可导出的账号，${check.blocking_count} 个账号被阻断`);
        return;
      }
      if (!check.ok || check.warning_count > 0) {
        setExportCheck(check);
        if (!check.ok) {
          toast.warning(`已跳过 ${check.blocking_count} 个阻断账号，继续导出 ${exportableEmails.length} 个可用账号`);
        } else {
          toast.warning(`检查完成，有 ${check.warning_count} 个账号需要注意，仍会导出`);
        }
      }
      await runExportDownload(proxy, exportableEmails);
    } catch (error) {
      const message = error instanceof Error ? error.message : "导出注册账号 sub2api JSON 失败";
      toast.error(message);
    } finally {
      setIsCheckingExport(false);
    }
  };
  const toggleRegisteredEmail = (email: string, checked: boolean) => {
    setSelectedRegisteredEmails((prev) => {
      if (checked) {
        return Array.from(new Set([...prev, email]));
      }
      return prev.filter((item) => item !== email);
    });
  };
  const toggleAllVisibleRegistered = (checked: boolean) => {
    setSelectedRegisteredEmails((prev) => {
      if (checked) {
        return Array.from(new Set([...prev, ...visibleEmails]));
      }
      return prev.filter((email) => !visibleEmails.includes(email));
    });
  };
  const handleDeleteRegisteredAccounts = async (emails: string[]) => {
    const targetEmails = Array.from(new Set(emails.map((email) => email.trim()).filter(Boolean)));
    if (targetEmails.length === 0) {
      toast.error("请先选择要删除的已注册号码");
      return;
    }
    const confirmed = window.confirm(`确定从注册机历史里删除 ${targetEmails.length} 个已注册号码吗？不会删除 chat 本地号池或 sub2api。`);
    if (!confirmed) {
      return;
    }
    const removed = await deleteRegisteredAccounts(targetEmails);
    if (removed > 0) {
      setSelectedRegisteredEmails((prev) => prev.filter((email) => !targetEmails.includes(email)));
      setExportCheck(null);
    }
  };
  const handleRecoverRegisteredAccounts = async (emails: string[]) => {
    const recoverableSet = new Set(recoverableVisibleEmails);
    const targetEmails = Array.from(new Set(emails.map((email) => email.trim()).filter((email) => email && recoverableSet.has(email))));
    if (targetEmails.length === 0) {
      toast.error("没有检测到需要恢复的已注册号码");
      return;
    }
    const recoverResult = await recoverRegisteredAccounts(targetEmails);
    const manualError = recoverResult.errors.find((item) => item.manual_code_required && item.session_id);
    if (manualError?.session_id) {
      setManualRecovery({
        sessionId: manualError.session_id,
        email: manualError.email,
        apiBase: manualError.api_base,
        code: "",
      });
    }
    if (recoverResult.recovered > 0) {
      setExportCheck((prev) => {
        if (!prev) return prev;
        const recoveredEmails = new Set(targetEmails.map((email) => email.toLowerCase()));
        const items = prev.items.filter((item) => !recoveredEmails.has(item.email.toLowerCase()));
        if (items.length === prev.items.length) return prev;
        const blocking_count = items.filter((item) => !item.exportable).length;
        const warning_count = items.filter((item) => item.warnings.length > 0).length;
        return {
          ...prev,
          items,
          total: items.length,
          ok: blocking_count === 0,
          blocking_count,
          warning_count,
        };
      });
    }
  };
  const handleCompleteManualRecovery = async () => {
    if (!manualRecovery) return;
    const ok = await completeManualRegisteredAccountRecovery(manualRecovery.sessionId, manualRecovery.code);
    if (ok) {
      setManualRecovery(null);
      setExportCheck(null);
    }
  };
  const handleImportRegisteredAccounts = async (emails: string[]) => {
    const targetEmails = Array.from(new Set(emails.map((email) => email.trim()).filter(Boolean)));
    if (targetEmails.length === 0) {
      toast.error("没有可导入的已注册号码");
      return;
    }
    await importRegisteredAccountsToLocalPool(targetEmails);
  };
  const copyText = async (value: string, label: string) => {
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      toast.success(`${label}已复制`);
    } catch {
      toast.error("复制失败");
    }
  };
  const proxyLabel = (account: (typeof visibleRegisteredAccounts)[number]) => {
    const proxy = account.proxy;
    if (proxy?.name) return proxy.name;
    if (proxy?.host && proxy?.port) return `${proxy.host}:${proxy.port}`;
    return account.proxy_key || "-";
  };
  const providerLabel = (account: (typeof visibleRegisteredAccounts)[number]) => {
    return account.mail_provider || account.mail_provider_ref || "-";
  };
  const apiBaseLabel = (account: (typeof visibleRegisteredAccounts)[number]) => {
    const mailbox = (account as { mailbox?: Record<string, unknown> | null }).mailbox || {};
    return String(mailbox.api_base || "-");
  };
  const renderRegisteredAccountsSearch = (className = "h-8 w-[180px] rounded-lg border-stone-200 bg-white pl-8 text-xs") => (
    <div className="relative">
      <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-stone-400" />
      <Input
        value={registeredEmailQuery}
        onChange={(event) => setRegisteredEmailQuery(event.target.value)}
        placeholder="搜索邮箱"
        className={className}
      />
    </div>
  );
  const registeredAccountsTable = (
    <table className="w-full min-w-[1080px] text-left text-xs">
      <thead className="sticky top-0 bg-stone-50 text-stone-500">
        <tr>
          <th className="w-10 px-3 py-2 font-medium">
            <Checkbox
              checked={allVisibleSelected}
              onCheckedChange={(checked) => toggleAllVisibleRegistered(Boolean(checked))}
            />
          </th>
          <th className="px-3 py-2 font-medium">邮箱</th>
          <th className="px-3 py-2 font-medium">密码</th>
          <th className="px-3 py-2 font-medium">注册方式</th>
          <th className="px-3 py-2 font-medium">API Base</th>
          <th className="px-3 py-2 font-medium">代理</th>
          <th className="px-3 py-2 font-medium">已导入本地池</th>
          <th className="px-3 py-2 font-medium">状态</th>
          <th className="px-3 py-2 font-medium">注册时间</th>
          <th className="w-14 px-3 py-2 font-medium">操作</th>
        </tr>
      </thead>
      <tbody>
        {visibleRegisteredAccounts.length === 0 ? (
          <tr>
            <td colSpan={10} className="px-3 py-6 text-center text-stone-500">暂无已注册号码</td>
          </tr>
        ) : (
          visibleRegisteredAccounts.map((account, index) => (
            <tr key={`${account.email}-${account.created_at || index}`} className="border-t border-stone-100 text-stone-700">
              <td className="px-3 py-2">
                <Checkbox
                  checked={selectedRegisteredEmails.includes(account.email)}
                  onCheckedChange={(checked) => toggleRegisteredEmail(account.email, Boolean(checked))}
                />
              </td>
              <td className="px-3 py-2">
                <button type="button" className="inline-flex max-w-[220px] items-center gap-1 truncate font-mono hover:text-stone-950" onClick={() => void copyText(account.email, "邮箱")}>
                  <span className="truncate">{account.email}</span>
                  <Copy className="size-3.5 shrink-0 text-stone-400" />
                </button>
              </td>
              <td className="px-3 py-2">
                <button type="button" className="inline-flex max-w-[180px] items-center gap-1 truncate font-mono hover:text-stone-950" onClick={() => void copyText(account.password, "密码")}>
                  <span className="truncate">{showRegisteredPasswords ? account.password : "••••••••••••"}</span>
                  <Copy className="size-3.5 shrink-0 text-stone-400" />
                </button>
              </td>
              <td className="whitespace-nowrap px-3 py-2 text-stone-500">
                {providerLabel(account)}
              </td>
              <td className="max-w-[180px] truncate px-3 py-2 font-mono text-stone-500" title={apiBaseLabel(account)}>
                {apiBaseLabel(account)}
              </td>
              <td className="max-w-[180px] truncate px-3 py-2 text-stone-500" title={String(proxyLabel(account))}>
                {proxyLabel(account)}
              </td>
              <td className="whitespace-nowrap px-3 py-2">
                <Badge variant={account.imported_to_local_pool ? "success" : "secondary"} className="rounded-md">
                  {account.imported_to_local_pool ? (account.local_pool_status || "已导入") : "未导入"}
                </Badge>
              </td>
              <td className="whitespace-nowrap px-3 py-2">
                <Badge variant={accountCredentialIssue(account) ? "danger" : accountMetadataIssue(account) ? "warning" : account.recovered ? "info" : "success"} className="rounded-md">
                  {accountCredentialIssue(account) ? (account.auth_failed ? "待登录" : "需恢复") : accountMetadataIssue(account) ? "待补ID" : account.recovered ? "已恢复" : "可导出"}
                </Badge>
                {account.error ? (
                  <Popover>
                    <PopoverTrigger asChild>
                      <button
                        type="button"
                        className="mt-1 block max-w-[180px] truncate text-left text-[11px] text-rose-500 underline-offset-2 hover:text-rose-700 hover:underline"
                      >
                        {account.error}
                      </button>
                    </PopoverTrigger>
                    <PopoverContent
                      align="start"
                      sideOffset={8}
                      className="w-[min(520px,calc(100vw-48px))] rounded-xl border-rose-100 p-0"
                      onOpenAutoFocus={(event) => event.preventDefault()}
                    >
                      <div className="border-b border-rose-100 px-3 py-2 text-xs font-medium text-rose-700">
                        错误详情
                      </div>
                      <div className="max-h-56 overflow-auto px-3 py-2 font-mono text-xs leading-5 break-all whitespace-pre-wrap text-stone-700">
                        {account.error}
                      </div>
                      <div className="flex justify-end border-t border-stone-100 px-3 py-2">
                        <Button
                          type="button"
                          variant="outline"
                          className="h-8 rounded-lg border-stone-200 bg-white px-2 text-xs text-stone-700"
                          onClick={() => void copyText(account.error || "", "错误信息")}
                        >
                          <Copy className="size-3.5" />
                          复制
                        </Button>
                      </div>
                    </PopoverContent>
                  </Popover>
                ) : null}
              </td>
              <td className="whitespace-nowrap px-3 py-2 text-stone-500">
                {account.created_at ? new Date(account.created_at).toLocaleString() : "-"}
              </td>
              <td className="px-3 py-2">
                <div className="flex items-center gap-1">
                  <button
                    type="button"
                    className="rounded-lg p-2 text-stone-400 transition hover:bg-emerald-50 hover:text-emerald-600 disabled:opacity-50"
                    onClick={() => void handleRecoverRegisteredAccounts([account.email])}
                    disabled={isSaving || !accountNeedsRecovery(account)}
                    title={accountMetadataIssue(account) ? "缺少 chatgpt_account_id 时请用导出前检查补齐，不需要重新登录" : "重新登录恢复凭据"}
                  >
                    <UserCheck className="size-4" />
                  </button>
                  <button
                    type="button"
                    className="rounded-lg p-2 text-stone-400 transition hover:bg-blue-50 hover:text-blue-600 disabled:opacity-50"
                    onClick={() => void handleImportRegisteredAccounts([account.email])}
                    disabled={isSaving || Boolean(account.imported_to_local_pool) || !Boolean(String((account as { access_token?: string }).access_token || "").trim())}
                    title="导入本地号池"
                  >
                    <UserPlus className="size-4" />
                  </button>
                  <button
                    type="button"
                    className="rounded-lg p-2 text-stone-400 transition hover:bg-rose-50 hover:text-rose-500 disabled:opacity-50"
                    onClick={() => void handleDeleteRegisteredAccounts([account.email])}
                    disabled={isSaving}
                    title="删除注册记录"
                  >
                    <Trash2 className="size-4" />
                  </button>
                </div>
              </td>
            </tr>
          ))
        )}
      </tbody>
    </table>
  );
  const checkSummary = exportCheck
    ? exportCheck.blocking_count > 0
      ? `${exportCheck.blocking_count} 个阻断`
      : exportCheck.warning_count > 0
        ? `${exportCheck.warning_count} 个提醒`
        : "检查通过"
    : "";
  const updateProviderType = (index: number, type: string) => {
    updateProvider(index, {
      type,
      enable: true,
      ...(type === "cloudmail_gen" ? { api_base: "", admin_email: "", admin_password: "", domain: [], subdomain: [], email_prefix: "" } : {}),
      ...(type === "cloudflare_temp_email" ? { api_base: "", admin_password: "", domain: [] } : {}),
      ...(type === "tempmail_lol" ? { api_key: "", domain: [] } : {}),
      ...(type === "moemail" ? { api_base: "", api_key: "", domain: [] } : {}),
      ...(type === "inbucket" ? { api_base: "", domain: [], random_subdomain: true } : {}),
      ...(type === "duckmail" ? { api_key: "", default_domain: "duckmail.sbs" } : {}),
      ...(type === "gptmail" ? { api_key: "", default_domain: "" } : {}),
      ...(type === "yyds_mail" ? { api_base: "https://maliapi.215.im/v1", api_key: "", domain: [], subdomain: "", wildcard: false } : {}),
      ...(type === "ddg_mail" ? { ddg_token: "", cf_inbox_jwt: "", cf_domain: [], admin_password: "" } : {}),
    });
  };

  return (
    <div className="grid h-[calc(100vh-132px)] min-h-[640px] items-stretch gap-0 overflow-hidden rounded-xl border border-stone-200 bg-white/70 xl:grid-cols-2">
      <Dialog open={Boolean(exportCheck)} onOpenChange={(open) => (!open ? setExportCheck(null) : null)}>
        <DialogContent showCloseButton={false} className="max-h-[82vh] max-w-3xl overflow-hidden rounded-2xl p-6">
          <DialogHeader className="gap-2">
            <DialogTitle>导出前检查</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              只检查必要字段和当前 access token 状态，不会使用 refresh token。
            </DialogDescription>
          </DialogHeader>
          <div className="max-h-[52vh] overflow-auto border border-stone-200">
            <table className="w-full min-w-[680px] text-left text-xs">
              <thead className="sticky top-0 bg-stone-50 text-stone-500">
                <tr>
                  <th className="px-3 py-2 font-medium">账号</th>
                  <th className="px-3 py-2 font-medium">结果</th>
                  <th className="px-3 py-2 font-medium">远端</th>
                  <th className="px-3 py-2 font-medium">说明</th>
                </tr>
              </thead>
              <tbody>
                {(exportCheck?.items || []).map((item) => {
                  const messages = [...item.errors, ...item.warnings];
                  return (
                    <tr key={item.email} className="border-t border-stone-100">
                      <td className="px-3 py-2 font-mono text-stone-700">{item.email}</td>
                      <td className="px-3 py-2">
                        <Badge variant={item.exportable ? "success" : "danger"} className="rounded-md">
                          {item.exportable ? "可导出" : "阻断"}
                        </Badge>
                      </td>
                      <td className="px-3 py-2 text-stone-500">{item.remote_status}</td>
                      <td className="px-3 py-2 text-stone-600">
                        {messages.length > 0 ? messages.join("；") : "检查通过"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <DialogFooter className="pt-2">
            <Button
              className="h-10 rounded-xl bg-stone-950 px-5 text-white hover:bg-stone-800"
              onClick={() => setExportCheck(null)}
            >
              知道了
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
              disabled={isSaving}
            >
              取消
            </Button>
            <Button
              type="button"
              className="h-10 rounded-xl bg-stone-950 px-5 text-white hover:bg-stone-800"
              onClick={() => void handleCompleteManualRecovery()}
              disabled={isSaving || (manualRecovery?.code.length || 0) !== 6}
            >
              {isSaving ? <LoaderCircle className="size-4 animate-spin" /> : null}
              提交验证码
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      <Dialog open={registeredAccountsOpen} onOpenChange={setRegisteredAccountsOpen}>
        <DialogContent className="max-h-[88vh] w-[min(96vw,1280px)] max-w-none grid-rows-[auto_auto_minmax(0,1fr)] overflow-hidden rounded-2xl p-6">
          <DialogHeader className="gap-2 pr-8">
            <DialogTitle>已注册号码</DialogTitle>
            <DialogDescription className="text-sm leading-6">
              注册结果会保留账号密码、OAuth 信息和本地日志；导出前会检查必要字段和 access token 当前状态，不会使用 refresh token。
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-wrap items-center justify-between gap-3 border-y border-stone-200 py-3">
            <div className="flex flex-wrap items-center gap-2">
              {renderRegisteredAccountsSearch("h-9 w-[260px] rounded-lg border-stone-200 bg-white pl-8 text-xs")}
              <Badge variant="secondary" className="rounded-md">
                {visibleRegisteredAccounts.length}/{scopedRegisteredAccounts.length}
              </Badge>
              {selectedVisibleEmails.length > 0 ? (
                <Badge variant="info" className="rounded-md">
                  已选 {selectedVisibleEmails.length}
                </Badge>
              ) : null}
            </div>
            <div className="flex flex-wrap items-center justify-end gap-2">
              <Button
                type="button"
                variant="outline"
                className="h-9 rounded-lg border-stone-200 bg-white px-3 text-stone-700"
                onClick={() => void exportSub2API(false)}
                disabled={visibleRegisteredAccounts.length === 0 || isCheckingExport}
              >
                {isCheckingExport ? <LoaderCircle className="size-4 animate-spin" /> : <Download className="size-4" />}
                导出
              </Button>
              <Button
                type="button"
                variant="outline"
                className="h-9 rounded-lg border-stone-200 bg-white px-3 text-stone-700"
                onClick={() => void exportSub2API(true)}
                disabled={visibleRegisteredAccounts.length === 0 || isCheckingExport}
              >
                {isCheckingExport ? <LoaderCircle className="size-4 animate-spin" /> : <Download className="size-4" />}
                导出+代理
              </Button>
              <Button
                type="button"
                variant="outline"
                className="h-9 rounded-lg border-emerald-200 bg-white px-3 text-emerald-700 hover:bg-emerald-50"
                onClick={() => void handleRecoverRegisteredAccounts(selectedVisibleEmails)}
                disabled={recoverableSelectedEmails.length === 0 || isSaving}
              >
                {isSaving ? <LoaderCircle className="size-4 animate-spin" /> : <UserCheck className="size-4" />}
                恢复所选
              </Button>
              <Button
                type="button"
                variant="outline"
                className="h-9 rounded-lg border-stone-200 bg-white px-3 text-stone-700"
                onClick={() => void backfillRegisteredMailMetadata()}
                disabled={isSaving || visibleRegisteredAccounts.length === 0}
              >
                {isSaving ? <LoaderCircle className="size-4 animate-spin" /> : <RotateCcw className="size-4" />}
                回填邮箱信息
              </Button>
              <Button
                type="button"
                variant="outline"
                className="h-9 rounded-lg border-blue-200 bg-white px-3 text-blue-700 hover:bg-blue-50"
                onClick={() => void handleImportRegisteredAccounts(importableSelectedEmails.length > 0 ? importableSelectedEmails : importableVisibleEmails)}
                disabled={isSaving || (importableSelectedEmails.length === 0 && importableVisibleEmails.length === 0)}
              >
                {isSaving ? <LoaderCircle className="size-4 animate-spin" /> : <UserPlus className="size-4" />}
                {importableSelectedEmails.length > 0 ? "导入所选" : "导入未入池"}
              </Button>
              <Button
                type="button"
                variant="outline"
                className="h-9 rounded-lg border-rose-200 bg-white px-3 text-rose-600 hover:bg-rose-50"
                onClick={() => void handleDeleteRegisteredAccounts(selectedVisibleEmails)}
                disabled={selectedVisibleEmails.length === 0 || isSaving}
              >
                {isSaving ? <LoaderCircle className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
                删除所选
              </Button>
              <Button
                type="button"
                variant="outline"
                className="h-9 rounded-lg border-stone-200 bg-white px-3 text-stone-700"
                onClick={() => setShowRegisteredPasswords((value) => !value)}
              >
                {showRegisteredPasswords ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                密码
              </Button>
            </div>
          </div>
          <div className="min-h-0 overflow-auto border border-stone-200 bg-white/70">
            {registeredAccountsTable}
          </div>
        </DialogContent>
      </Dialog>
      <section className="space-y-4 overflow-y-auto border-b border-stone-200 p-4 xl:border-r xl:border-b-0">
          <div className="flex items-start justify-between gap-3">
            <div className="flex items-center gap-3">
              <div className="flex size-9 items-center justify-center rounded-md bg-stone-100">
                <UserPlus className="size-5 text-stone-600" />
              </div>
              <div>
                <h2 className="text-lg font-semibold tracking-tight">注册配置</h2>
              </div>
            </div>
            <Button className="h-9 rounded-xl bg-stone-950 px-4 text-white hover:bg-stone-800" onClick={() => void save()} disabled={isSaving || config.enabled}>
              {isSaving ? <LoaderCircle className="size-4 animate-spin" /> : <Save className="size-4" />}
              保存配置
            </Button>
          </div>

          <div className="grid gap-4 md:grid-cols-3">
            <div className="space-y-2">
              <label className="text-sm text-stone-700">注册模式</label>
              <Select value={config.mode || "total"} onValueChange={(value) => setMode(value as "total" | "quota" | "available")} disabled={config.enabled}>
                <SelectTrigger className="h-10 rounded-xl border-stone-200 bg-white">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="total">注册总数</SelectItem>
                  <SelectItem value="quota">号池剩余额度</SelectItem>
                  <SelectItem value="available">可用账号数量</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <label className="text-sm text-stone-700">注册总数</label>
              <Input value={String(config.total)} onChange={(event) => setTotal(event.target.value)} className="h-10 rounded-xl border-stone-200 bg-white" disabled={config.enabled || config.mode !== "total"} />
            </div>
            <div className="space-y-2">
              <label className="text-sm text-stone-700">线程数</label>
              <Input value={String(config.threads)} onChange={(event) => setThreads(event.target.value)} className="h-10 rounded-xl border-stone-200 bg-white" disabled={config.enabled} />
            </div>
            <div className="space-y-2">
              <label className="text-sm text-stone-700">注册代理</label>
              <Input value={config.proxy} onChange={(event) => setProxy(event.target.value)} placeholder="http://127.0.0.1:7890" className="h-10 rounded-xl border-stone-200 bg-white" disabled={config.enabled} />
            </div>
            <div className="space-y-2">
              <label className="text-sm text-stone-700">目标剩余额度</label>
              <Input value={String(config.target_quota || "")} onChange={(event) => setTargetQuota(event.target.value)} className="h-10 rounded-xl border-stone-200 bg-white" disabled={config.enabled || config.mode !== "quota"} />
            </div>
            <div className="space-y-2">
              <label className="text-sm text-stone-700">目标可用账号</label>
              <Input value={String(config.target_available || "")} onChange={(event) => setTargetAvailable(event.target.value)} className="h-10 rounded-xl border-stone-200 bg-white" disabled={config.enabled || config.mode !== "available"} />
            </div>
            <div className="space-y-2">
              <label className="text-sm text-stone-700">检查间隔（秒）</label>
              <Input value={String(config.check_interval || "")} onChange={(event) => setCheckInterval(event.target.value)} className="h-10 rounded-xl border-stone-200 bg-white" disabled={config.enabled || config.mode === "total"} />
            </div>
            <label className="flex items-center gap-3 pt-8 text-sm text-stone-700">
              <Checkbox checked={Boolean(config.add_to_local_pool)} onCheckedChange={(checked) => setAddToLocalPool(Boolean(checked))} disabled={config.enabled} />
              注册后加入本地号池
            </label>
          </div>
          <div className="border border-stone-200 bg-white/70 px-3 py-2 text-xs leading-5 text-stone-600">
            {addToLocalPool
              ? "当前配置：注册成功后保存完整登录信息、写入本地日志，并同时加入 chat 本地号池。"
              : "当前配置：注册成功后保存完整登录信息和本地日志，不自动进入 chat 本地号池。"}
          </div>

          <div className="space-y-3 border-t border-stone-200 pt-3">
            <div className="flex items-center justify-between gap-3">
              <div>
                <h3 className="text-sm font-semibold text-stone-800">邮箱配置</h3>
                <p className="mt-1 text-xs text-stone-500">可配置多个 provider，按启用顺序轮换。</p>
              </div>
              <Button type="button" variant="outline" className="h-9 rounded-xl border-stone-200 bg-white px-3 text-stone-700" onClick={addProvider} disabled={config.enabled}>
                <Plus className="size-4" />
                添加
              </Button>
            </div>

            <div className="grid gap-4 md:grid-cols-3">
              <div className="space-y-2">
                <label className="text-sm text-stone-700">请求超时</label>
                <Input value={String(config.mail.request_timeout || "")} onChange={(event) => setMailField("request_timeout", event.target.value)} className="h-10 rounded-xl border-stone-200 bg-white" disabled={config.enabled} />
              </div>
              <div className="space-y-2">
                <label className="text-sm text-stone-700">等待验证码超时</label>
                <Input value={String(config.mail.wait_timeout || "")} onChange={(event) => setMailField("wait_timeout", event.target.value)} className="h-10 rounded-xl border-stone-200 bg-white" disabled={config.enabled} />
              </div>
              <div className="space-y-2">
                <label className="text-sm text-stone-700">轮询间隔</label>
                <Input value={String(config.mail.wait_interval || "")} onChange={(event) => setMailField("wait_interval", event.target.value)} className="h-10 rounded-xl border-stone-200 bg-white" disabled={config.enabled} />
              </div>
            </div>

            <div className="space-y-3">
              {providers.map((provider, index) => {
                const type = String(provider.type || "tempmail_lol");
                const domains = Array.isArray(provider.domain) ? provider.domain.map(String).join("\n") : "";
                const subdomains = Array.isArray(provider.subdomain) ? provider.subdomain.map(String).join("\n") : "";
                return (
                  <div key={index} className="space-y-3 border-t border-stone-200 pt-3 first:border-t-0 first:pt-0">
                    <div className="flex items-center justify-between gap-3">
                      <label className="flex items-center gap-3 text-sm text-stone-700">
                        <Checkbox checked={Boolean(provider.enable)} onCheckedChange={(checked) => updateProvider(index, { enable: Boolean(checked) })} disabled={config.enabled} />
                        启用
                      </label>
                      <button type="button" className="rounded-lg p-2 text-stone-400 transition hover:bg-rose-50 hover:text-rose-500 disabled:opacity-50" onClick={() => deleteProvider(index)} disabled={config.enabled || providers.length <= 1} title="删除 provider">
                        <Trash2 className="size-4" />
                      </button>
                    </div>

                    <div className="grid gap-4 md:grid-cols-2">
                      <div className="space-y-2">
                        <label className="text-sm text-stone-700">类型</label>
                        <Select value={type} onValueChange={(value) => updateProviderType(index, value)} disabled={config.enabled}>
                          <SelectTrigger className="h-10 rounded-xl border-stone-200 bg-white">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="cloudmail_gen">cloudmail_gen</SelectItem>
                            <SelectItem value="cloudflare_temp_email">cloudflare_temp_email</SelectItem>
                            <SelectItem value="tempmail_lol">tempmail_lol</SelectItem>
                            <SelectItem value="moemail">moemail</SelectItem>
                            <SelectItem value="inbucket">inbucket_mail</SelectItem>
                            <SelectItem value="duckmail">duckmail</SelectItem>
                            <SelectItem value="gptmail">gptmail(未测试)</SelectItem>
                            <SelectItem value="yyds_mail">yyds_mail</SelectItem>
                            <SelectItem value="ddg_mail">ddg_mail (DDG邮箱+CF中转)</SelectItem>
                          </SelectContent>
                        </Select>
                      </div>
                      {type === "cloudmail_gen" || type === "cloudflare_temp_email" || type === "moemail" || type === "inbucket" || type === "yyds_mail" || type === "ddg_mail" ? (
                        <>
                          <div className="space-y-2">
                            <label className="text-sm text-stone-700">{type === "cloudmail_gen" ? "CloudMail URL" : "API Base"}</label>
                            <Input value={String(provider.api_base || "")} onChange={(event) => updateProvider(index, { api_base: event.target.value })} className="h-10 rounded-xl border-stone-200 bg-white" disabled={config.enabled} />
                          </div>
                          {type === "cloudmail_gen" ? (
                            <>
                              <div className="space-y-2">
                                <label className="text-sm text-stone-700">管理员邮箱</label>
                                <Input value={String(provider.admin_email || "")} onChange={(event) => updateProvider(index, { admin_email: event.target.value })} className="h-10 rounded-xl border-stone-200 bg-white" disabled={config.enabled} />
                              </div>
                              <div className="space-y-2">
                                <label className="text-sm text-stone-700">管理员密码</label>
                                <Input value={String(provider.admin_password || "")} onChange={(event) => updateProvider(index, { admin_password: event.target.value })} className="h-10 rounded-xl border-stone-200 bg-white" disabled={config.enabled} />
                              </div>
                            </>
                          ) : null}
                          {type === "cloudflare_temp_email" || type === "ddg_mail" ? (
                            <div className="space-y-2">
                              <label className="text-sm text-stone-700">Admin Password</label>
                              <Input value={String(provider.admin_password || "")} onChange={(event) => updateProvider(index, { admin_password: event.target.value })} className="h-10 rounded-xl border-stone-200 bg-white" disabled={config.enabled} />
                            </div>
                          ) : null}
                        </>
                      ) : null}
                      {type === "ddg_mail" ? (
                        <>
                        <div className="space-y-2">
                          <label className="text-sm text-stone-700">DDG Token <span className="text-red-400">*</span></label>
                          <Input value={String(provider.ddg_token || "")} onChange={(event) => updateProvider(index, { ddg_token: event.target.value })} className="h-10 rounded-xl border-stone-200 bg-white" disabled={config.enabled} placeholder="DuckDuckGo Email Protection 的 Bearer Token" />
                        </div>
                        <div className="space-y-2">
                          <label className="text-sm text-stone-700">CF Inbox JWT <span className="text-red-400">*</span></label>
                          <Input value={String(provider.cf_inbox_jwt || "")} onChange={(event) => updateProvider(index, { cf_inbox_jwt: event.target.value })} className="h-10 rounded-xl border-stone-200 bg-white" disabled={config.enabled} placeholder="CF 临时邮箱后端的固定收件箱 JWT（DDG 转发目标）" />
                        </div>
                        <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
                          <p className="font-medium mb-1">使用说明</p>
                          <ol className="list-decimal list-inside space-y-0.5">
                            <li>先在 <a href="https://duckduckgo.com/email/" target="_blank" className="underline">DuckDuckGo Email Protection</a> 登录并设置转发目标为 CF 收件箱地址</li>
                            <li>DDG Token 从浏览器 DevTools → Network → quack.duckduckgo.com 请求中获取 <code className="bg-amber-100 px-1 rounded">Authorization: Bearer</code></li>
                            <li>CF Inbox JWT 从 CF 临时邮箱后端创建固定收件箱后获取</li>
                            <li>所有 @duck.com 别名收到的邮件会转发到同一个 CF 收件箱，系统按 To: 头自动匹配</li>
                          </ol>
                        </div>
                        </>
                      ) : null}
                      {type === "inbucket" ? (
                        <label className="flex items-center gap-3 pt-8 text-sm text-stone-700">
                          <Checkbox checked={Boolean(provider.random_subdomain ?? true)} onCheckedChange={(checked) => updateProvider(index, { random_subdomain: Boolean(checked) })} disabled={config.enabled} />
                          启用随机子域名
                        </label>
                      ) : null}
                      {type === "tempmail_lol" || type === "moemail" || type === "duckmail" || type === "gptmail" || type === "yyds_mail" ? (
                        <div className="space-y-2">
                          <label className="text-sm text-stone-700">API Key</label>
                          <Input value={String(provider.api_key || "")} onChange={(event) => updateProvider(index, { api_key: event.target.value })} className="h-10 rounded-xl border-stone-200 bg-white" disabled={config.enabled} />
                        </div>
                      ) : null}
                      {type === "duckmail" || type === "gptmail" ? (
                        <div className="space-y-2">
                          <label className="text-sm text-stone-700">Default Domain</label>
                          <Input value={String(provider.default_domain || "")} onChange={(event) => updateProvider(index, { default_domain: event.target.value })} placeholder={type === "duckmail" ? "duckmail.sbs" : ""} className="h-10 rounded-xl border-stone-200 bg-white" disabled={config.enabled} />
                        </div>
                      ) : null}
                      {type === "yyds_mail" ? (
                        <>
                          <div className="space-y-2">
                            <label className="text-sm text-stone-700">Subdomain</label>
                            <Input value={String(provider.subdomain || "")} onChange={(event) => updateProvider(index, { subdomain: event.target.value })} className="h-10 rounded-xl border-stone-200 bg-white" disabled={config.enabled} />
                          </div>
                          <label className="flex items-center gap-3 pt-8 text-sm text-stone-700">
                            <Checkbox checked={Boolean(provider.wildcard)} onCheckedChange={(checked) => updateProvider(index, { wildcard: Boolean(checked) })} disabled={config.enabled} />
                            Wildcard
                          </label>
                        </>
                      ) : null}
                    </div>

                    {type === "cloudmail_gen" || type === "tempmail_lol" || type === "cloudflare_temp_email" || type === "moemail" || type === "inbucket" || type === "yyds_mail" || type === "ddg_mail" ? (
                      <div className="space-y-2">
                        <label className="text-sm text-stone-700">{type === "cloudmail_gen" ? "邮箱域名" : type === "inbucket" ? "基础域名列表" : "Domain"}</label>
                        <Textarea value={domains} onChange={(event) => updateProvider(index, { domain: event.target.value.split(/[\n,]/).map((item) => item.trim()) })} placeholder={type === "cloudmail_gen" ? "每行一个域名，留空则使用服务默认域名" : type === "inbucket" ? "每行一个基础域名，系统会自动生成随机子域名" : type === "moemail" ? "每行一个域名" : "每行一个域名，留空则使用服务默认域名"} className="min-h-20 rounded-xl border-stone-200 bg-white font-mono text-xs" disabled={config.enabled} />
                      </div>
                    ) : null}
                    {type === "cloudmail_gen" ? (
                      <div className="space-y-2">
                        <label className="text-sm text-stone-700">子域名（支持多个）</label>
                        <Textarea value={subdomains} onChange={(event) => updateProvider(index, { subdomain: event.target.value.split(/[\n,]/).map((item) => item.trim()) })} placeholder="每行一个子域名前缀，留空则直接使用主域名" className="min-h-20 rounded-xl border-stone-200 bg-white font-mono text-xs" disabled={config.enabled} />
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          </div>

      </section>

      <section className="flex min-h-0 flex-col p-4">
        <div className="space-y-3">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h2 className="text-lg font-semibold tracking-tight">运行结果</h2>
                <p className="mt-1 text-sm text-stone-500">SSE 实时推送当前状态。</p>
              </div>
              <Badge variant={config.enabled ? "success" : "secondary"} className="rounded-md">
                {config.enabled ? "运行中" : "已停止"}
              </Badge>
            </div>
            <div className="grid grid-cols-4 gap-2">
              {[
                ["成功 / 成功率", `${stats.success} / ${stats.success_rate || 0}%`],
                ["失败", stats.fail],
                ["完成", stats.done],
                ["运行 / 线程", `${stats.running} / ${stats.threads}`],
                ["运行时间", `${stats.elapsed_seconds || 0}s`],
                ["平均注册单个", `${stats.avg_seconds || 0}s`],
                ["当前额度", stats.current_quota || 0],
                ["正常账号", stats.current_available || 0],
              ].map(([label, value]) => (
                <div key={label} className="border border-stone-200 bg-white/70 px-3 py-2">
                  <div className="text-xs text-stone-400">{label}</div>
                  <div className="mt-1 text-base font-semibold text-stone-800">{value}</div>
                </div>
              ))}
            </div>
            <div className="grid grid-cols-2 gap-2 md:grid-cols-3">
              <Button className="h-10 rounded-xl bg-stone-950 px-3 text-white hover:bg-stone-800" onClick={() => void toggle()} disabled={isSaving}>
                {isSaving ? <LoaderCircle className="size-4 animate-spin" /> : config.enabled ? <Square className="size-4" /> : <Play className="size-4" />}
                {config.enabled ? "停止" : "启动"}
              </Button>
              <Button variant="outline" className="h-10 rounded-xl border-stone-200 bg-white px-3 text-stone-700" onClick={() => void reset()} disabled={isSaving || config.enabled}>
                <RotateCcw className="size-4" />
                重置
              </Button>
              <Button variant="outline" className="h-10 rounded-xl border-stone-200 bg-white px-3 text-stone-700" onClick={() => void save()} disabled={isSaving || config.enabled}>
                <Save className="size-4" />
                保存
              </Button>
            </div>
            <div className="flex items-center gap-2 border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              <AlertTriangle className="size-4 shrink-0" />
              启动之前注意先保存配置；sub2api 导出入口已移到“已注册号码”管理弹窗内。
            </div>
            {exportCheck ? (
              <div
                className={`flex items-center justify-between gap-3 border px-3 py-2 text-xs ${
                  exportCheck.blocking_count > 0
                    ? "border-rose-200 bg-rose-50 text-rose-700"
                    : "border-blue-200 bg-blue-50 text-blue-700"
                }`}
              >
                <span>{checkSummary}</span>
                <Button
                  type="button"
                  variant="ghost"
                  className="h-7 rounded-lg px-2 text-current hover:bg-white/50"
                  onClick={() => setExportCheck(null)}
                >
                  关闭
                </Button>
              </div>
            ) : null}
        </div>

        <div className="mt-4 grid min-h-0 flex-1 grid-rows-[auto_minmax(260px,1fr)] gap-4 overflow-hidden border-t border-stone-200 pt-4">
          <div className="space-y-3">
            <div className="flex items-center justify-between gap-3">
              <div>
                <h3 className="text-sm font-semibold text-stone-900">已注册号码</h3>
                <p className="mt-1 text-xs text-stone-500">注册结果会保留账号密码、OAuth 信息和本地日志；未鉴权成功的账号会保留账号密码供兜底登录。</p>
              </div>
              <div className="flex flex-wrap items-center justify-end gap-2">
                {renderRegisteredAccountsSearch()}
                <Badge variant="secondary" className="rounded-md">
                  {visibleRegisteredAccounts.length}/{scopedRegisteredAccounts.length}
                </Badge>
                {selectedVisibleEmails.length > 0 ? (
                  <Badge variant="info" className="rounded-md">
                    已选 {selectedVisibleEmails.length}
                  </Badge>
                ) : null}
                <Button
                  type="button"
                  variant="outline"
                  className="h-8 rounded-lg border-stone-200 bg-white px-2 text-stone-700"
                  onClick={() => setRegisteredAccountsOpen(true)}
                >
                  <Maximize2 className="size-4" />
                  管理/导出
                </Button>
              </div>
            </div>
            <div className="grid gap-2 sm:grid-cols-3">
              <div className="border border-stone-200 bg-white/70 px-3 py-2">
                <div className="text-xs text-stone-400">当前范围</div>
                <div className="mt-1 text-base font-semibold text-stone-800">{scopedRegisteredAccounts.length}</div>
              </div>
              <div className="border border-stone-200 bg-white/70 px-3 py-2">
                <div className="text-xs text-stone-400">需恢复</div>
                <div className="mt-1 text-base font-semibold text-stone-800">{recoverableVisibleEmails.length}</div>
              </div>
              <div className="border border-stone-200 bg-white/70 px-3 py-2">
                <div className="text-xs text-stone-400">未入本地池</div>
                <div className="mt-1 text-base font-semibold text-stone-800">{importableVisibleEmails.length}</div>
              </div>
            </div>
          </div>

          <div className="flex min-h-0 flex-col space-y-3 overflow-hidden">
            <div className="flex items-center justify-between">
              <div>
                <h3 className="text-sm font-semibold text-stone-900">实时日志</h3>
                <p className="mt-1 text-xs text-amber-700">遇到 HTTP 状态码 400 等错误，基本是邮箱滥用被封，需要更换新的域名邮箱。</p>
              </div>
              <Badge variant="secondary" className="rounded-md">
                {logs.length}
              </Badge>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto border border-stone-200 bg-white/70 p-3 font-mono text-xs leading-6">
              {logs.length === 0 ? (
                <div className="text-stone-500">暂无日志</div>
              ) : (
                logs.slice().reverse().map((item, index) => (
                  <div key={`${item.time}-${index}`} className={item.level === "red" ? "text-rose-600" : item.level === "green" ? "text-emerald-700" : item.level === "yellow" ? "text-amber-700" : "text-stone-700"}>
                    <span className="text-stone-400">{new Date(item.time).toLocaleTimeString()}</span>
                    <span className="pl-2">{item.text}</span>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}
