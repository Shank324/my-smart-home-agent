<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'

type Role = 'home_user' | 'repair_staff'
type Status = 'open' | 'assigned' | 'in_progress' | 'resolved' | 'cancelled'
type User = { id: number; username: string; display_name: string; role: Role; home_id: number | null }
type Device = { id: number; name: string; room: string; type: string; online: boolean; active: boolean; value: string; power_consumption: number; attributes: Record<string, number | string> }
type AttributeSpec = { type: 'int' | 'enum'; min?: number; max?: number; choices?: string[]; default: number | string; label: string }
type DeviceTypeInfo = {
  key: string
  label: string
  attributes: Record<string, AttributeSpec>
  attribute_defaults: Record<string, number | string>
  status_default: string
  power_default: number
  status_template: string | null
  status_from_attributes: boolean
}
type Scene = { id: number; name: string; description: string; builtin: boolean; actions: Array<{ device_id: number; action: 'on' | 'off' }> }
type Order = { id: number; title: string; description: string; status: Status; home_id: number; created_at: string; assigned_to: number | null; assigned_at: string | null; updated_at: string | null; message_count: number; priority: string; device_id: number | null; device_name: string | null; cost: number | null; duration_minutes: number | null; resolved_at: string | null }
type OrderMessage = { id: number; work_order_id: number; sender_id: number; message: string; created_at: string }
type Section = 'overview' | 'devices' | 'scenes' | 'stats' | 'orders'

const API_BASE = 'http://localhost:8000'
const TOKEN_KEY = 'smart_home_token'
const token = ref(localStorage.getItem(TOKEN_KEY) || '')
const user = ref<User | null>(null)
const devices = ref<Device[]>([])
const scenes = ref<Scene[]>([])
const orders = ref<Order[]>([])
const orderMessages = ref<Record<number, OrderMessage[]>>({})
const selectedSection = ref<Section>('overview')
const selectedOrderId = ref<number | null>(null)
const mode = ref<'login' | 'register'>('login')
const error = ref('')
const message = ref('')
const busy = ref(false)
const chatDraft = ref('')
const completionForm = ref({ cost: '', duration: '' })
const form = ref({ username: '', password: '', display_name: '', home_name: '', role: 'home_user' as Role, phone: '', specialty: '', experience_years: '' })
const orderForm = ref({ title: '', description: '', device_id: '', priority: 'medium' })
const deviceForm = ref({ id: null as number | null, name: '', room: '', type: 'light', online: true, active: false, value: '0%', power_consumption: 0, attributes: {} as Record<string, number | string> })
const deviceTypes = ref<DeviceTypeInfo[]>([])
const enumLabels = ref<Record<string, Record<string, string>>>({})
const roomOptions = ['客厅', '餐厅', '主卧', '次卧', '书房', '厨房', '卫生间', '阳台', '玄关']
const sceneForm = ref({ id: null as number | null, name: '', description: '', actions: [] as Array<{ device_id: number; action: 'on' | 'off' }> })
const sceneActionDraft = ref({ device_id: '', action: 'on' as 'on' | 'off' })
const socket = ref<WebSocket | null>(null)

// ---- 数据统计（视图 / 复杂查询）----
type DeviceStats = { home_name: string; device_total: number; device_online: number; device_active: number; power_total: number; power_active: number }
type RoomStat = { room: string; device_count: number }
type CrossTypeRow = { room: string; device_a: string; type_a: string; device_b: string; type_b: string }
type StatsPayload = {
  device_stats: DeviceStats
  rooms_above_average: RoomStat[]
  cross_type_rooms: CrossTypeRow[]
  top_scene: { id: number; name: string; description: string; action_count: number } | null
  view: Record<string, unknown> | null
  view_source: string
}
type TimelineEvent = { from: Status; to: Status; actor_id: number | null; at: string }
const stats = ref<StatsPayload | null>(null)
const timeline = ref<TimelineEvent[]>([])
const powerActiveWatts = computed(() => Math.round(stats.value?.device_stats.power_active ?? 0))

// ---- AI 客服 ----
type Citation = { source: string; section: string }
type ProposalAction = { device_id: number; device_name: string; room: string; action: string; attributes: Record<string, number | string>; label: string }
type SceneProposal = { name: string; description: string; rationale: string; actions: ProposalAction[]; missing: string[] }
type WorkOrderDevice = { device_id: number; name: string; room: string; type: string; online: boolean; active: boolean; value: string; attributes: Record<string, number | string> }
type WorkOrderDraft = { title: string; description: string; priority: string; device_id: number | null; device: WorkOrderDevice | null }
type AssistantMessage = { role: 'user' | 'assistant'; content: string; citations?: Citation[]; proposal?: SceneProposal; workOrder?: WorkOrderDraft; applied?: boolean; pending?: boolean; intent?: string; grounded?: boolean; warnings?: string[] }
type AgentSessionInfo = { id: number; title: string; status: string; message_count?: number }
const assistantOpen = ref(false)
const assistantBusy = ref(false)
const assistantDraft = ref('')
const assistantMessages = ref<AssistantMessage[]>([])
const agentSessions = ref<AgentSessionInfo[]>([])
const activeSessionId = ref<number | null>(null)
const assistantBox = ref<HTMLElement | null>(null)
const suggestions = ['客厅空调开不起来怎么办？', '我家现在有哪些设备？', '厨房烟雾报警了怎么办？']

const isHome = computed(() => user.value?.role === 'home_user')
const statusText: Record<Status, string> = { open: '待处理', assigned: '已分派', in_progress: '处理中', resolved: '已解决', cancelled: '已取消' }
const priorityText: Record<string, string> = { low: '低', medium: '普通', high: '高', urgent: '紧急' }
type TechnicianProfile = {
  display_name: string
  specialty: string | null
  phone: string | null
  experience_years: number | null
  rating: number | null
  available: boolean | null
  stats: { total: number; assigned: number; in_progress: number; resolved: number }
  available_orders: number
}
const technician = ref<TechnicianProfile | null>(null)
const pending = computed(() => orders.value.filter((order) => !['resolved', 'cancelled'].includes(order.status)).length)
const activeOrder = computed(() => orders.value.find((order) => order.id === selectedOrderId.value) ?? null)
const currentTypeInfo = computed(() => deviceTypes.value.find((item) => item.key === deviceForm.value.type) ?? null)
const attributeFields = computed(() =>
  Object.entries(currentTypeInfo.value?.attributes ?? {}).map(([key, spec]) => ({ key, spec })))

const DEVICE_ICONS: Record<string, string> = {
  air_conditioner: '❄️',
  light: '💡',
  tv: '📺',
  sensor: '📡',
  water_heater: '🔥',
  other: '🔌',
}
function deviceIcon(type: string) {
  return DEVICE_ICONS[type] ?? '📦'
}
const onlineCount = computed(() => devices.value.filter((device) => device.online).length)

watch(token, (value) => value ? localStorage.setItem(TOKEN_KEY, value) : localStorage.removeItem(TOKEN_KEY))

// 有结构化参数的设备，状态值跟着参数走，保证列表展示和参数永远一致
watch(() => deviceForm.value.attributes, (attributes) => {
  const info = typeInfo(deviceForm.value.type)
  if (!info?.status_from_attributes) return
  deviceForm.value.value = deriveStatus(info, attributes)
}, { deep: true })

function notify(text: string) {
  message.value = text
  window.setTimeout(() => { message.value = '' }, 2600)
}

function logout() {
  socket.value?.close()
  socket.value = null
  token.value = ''
  user.value = null
  devices.value = []
  scenes.value = []
  orders.value = []
  orderMessages.value = {}
  selectedOrderId.value = null
  selectedSection.value = 'overview'
  technician.value = null
  stats.value = null
  timeline.value = []
}

async function api(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers)
  if (token.value) headers.set('Authorization', `Bearer ${token.value}`)
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers })
  if (response.status === 401) {
    notify('登录已失效，请重新登录')
    logout()
    throw new Error('登录已失效')
  }
  return response
}

async function loadMessages(orderId: number) {
  const response = await api(`/api/work-orders/${orderId}/messages`)
  if (!response.ok) return
  orderMessages.value[orderId] = await response.json()
}

async function loadTimeline(orderId: number) {
  const response = await api(`/api/work-orders/${orderId}/timeline`)
  if (!response.ok) { timeline.value = []; return }
  timeline.value = (await response.json()).events || []
}

async function loadStats() {
  const response = await api('/api/stats/overview')
  if (!response.ok) { notify('统计数据加载失败'); return }
  stats.value = await response.json()
}

async function loadTechnician() {
  if (isHome.value) { technician.value = null; return }
  const response = await api('/api/technician/me')
  if (!response.ok) return
  technician.value = await response.json()
}

function openStats() {
  selectedSection.value = 'stats'
  void loadStats()
}

async function loadData() {
  if (!user.value) return
  const requests: Promise<Response>[] = [api('/api/work-orders')]
  if (isHome.value) requests.push(api('/api/devices'), api('/api/scenes'))
  const responses = await Promise.all(requests)
  orders.value = await responses[0].json()
  if (selectedOrderId.value === null && orders.value[0]) selectedOrderId.value = orders.value[0].id
  if (selectedOrderId.value) await loadMessages(selectedOrderId.value)
  if (selectedOrderId.value) await loadTimeline(selectedOrderId.value)
  if (isHome.value) {
    devices.value = await responses[1].json()
    scenes.value = await responses[2].json()
  } else {
    await loadTechnician()
  }
}

function connectSocket() {
  socket.value?.close()
  if (!token.value || !user.value) return
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
  const ws = new WebSocket(`${scheme}://localhost:8000/ws/events?token=${encodeURIComponent(token.value)}`)
  ws.onmessage = async (event) => {
    const payload = JSON.parse(event.data)
    if (payload.event === 'device_updated') {
      const nextDevice = payload.data as Device
      devices.value = devices.value.map((device) => device.id === nextDevice.id ? nextDevice : device)
      return
    }
    await loadData()
    if (payload.event === 'work_order_message' && payload.data?.work_order_id) {
      await loadMessages(payload.data.work_order_id)
    }
  }
  socket.value = ws
}

async function hydrate() {
  if (!token.value) return
  try {
    user.value = await (await api('/api/me')).json()
    selectedSection.value = isHome.value ? 'overview' : 'orders'
    await loadData()
    await loadTechnician()
    connectSocket()
  } catch {
    logout()
  }
}

async function submitAuth() {
  busy.value = true; error.value = ''
  try {
    if (mode.value === 'register') {
      const registration = await fetch(`${API_BASE}/api/auth/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: form.value.username,
          password: form.value.password,
          display_name: form.value.display_name,
          home_name: form.value.home_name || '我的家庭',
          role: form.value.role,
          phone: form.value.phone || null,
          specialty: form.value.specialty || null,
          experience_years: form.value.experience_years === '' ? null : Number(form.value.experience_years),
        })
      })
      if (!registration.ok) {
        const detail = await registration.json().catch(() => ({}))
        throw new Error(typeof detail.detail === 'string' ? detail.detail : '注册失败')
      }
    }
    const response = await fetch(`${API_BASE}/api/auth/login`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: form.value.username, password: form.value.password }) })
    const payload = await response.json()
    if (!response.ok) throw new Error(payload.detail || '登录失败')
    token.value = payload.access_token
    user.value = payload.user
    selectedSection.value = isHome.value ? 'overview' : 'orders'
    await loadDeviceTypes()
    await loadData()
    await loadTechnician()
    connectSocket()
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '请求失败' }
  finally { busy.value = false }
}

async function loadDeviceTypes() {
  try {
    const response = await fetch(`${API_BASE}/api/device-types`)
    if (!response.ok) return
    const payload = await response.json()
    deviceTypes.value = payload.types || []
    enumLabels.value = payload.enum_labels || {}
    if (deviceForm.value.id === null) applyTypeDefaults()
  } catch {
    // 类型目录拿不到时表单仍可用，只是退化成纯文本输入
  }
}

function typeInfo(type: string) {
  return deviceTypes.value.find((item) => item.key === type) ?? null
}

/** 有状态模板的设备，状态值由参数推导，避免用户在两处各填一遍 */
function deriveStatus(info: DeviceTypeInfo | null, attributes: Record<string, number | string>) {
  if (!info?.status_template) return ''
  return info.status_template.replace(/\{(\w+)\}/g, (_, key: string) => String(attributes[key] ?? '--'))
}

function applyTypeDefaults() {
  const info = typeInfo(deviceForm.value.type)
  const attrs: Record<string, number | string> = {}
  for (const [key, spec] of Object.entries(info?.attributes ?? {})) {
    attrs[key] = spec.default
  }
  deviceForm.value.attributes = attrs
  deviceForm.value.power_consumption = info?.power_default ?? 0
  deviceForm.value.value = deriveStatus(info, attrs) || info?.status_default || '--'
}

function typeLabel(type: string) {
  return deviceTypes.value.find((item) => item.key === type)?.label ?? type
}

function attributeSummary(device: Device) {
  const info = deviceTypes.value.find((item) => item.key === device.type)
  if (!info) return ''
  return Object.entries(info.attributes)
    .map(([key, spec]) => {
      const raw = (device.attributes || {})[key]
      if (raw === undefined || raw === null || raw === '') return ''
      const shown = spec.type === 'enum' ? (enumLabels.value[key]?.[String(raw)] ?? String(raw)) : String(raw)
      return `${spec.label.replace(/\(.*\)$/, '')} ${shown}`
    })
    .filter(Boolean)
    .join(' · ')
}

function resetDeviceEditor() {
  deviceForm.value = {
    id: null, name: '', room: '', type: 'light', online: true, active: false,
    value: '0%', power_consumption: 0, attributes: {},
  }
  applyTypeDefaults()
}

async function saveDevice() {
  const payload = {
    name: deviceForm.value.name.trim(),
    room: deviceForm.value.room.trim(),
    type: deviceForm.value.type,
    online: deviceForm.value.online,
    active: deviceForm.value.active,
    value: deviceForm.value.value.trim() || '0%',
    power_consumption: Number(deviceForm.value.power_consumption) || 0,
    attributes: deviceForm.value.attributes,
  }
  if (!payload.name || !payload.room) {
    notify('请填写设备名称和所在房间')
    return
  }
  const editing = deviceForm.value.id !== null
  try {
    const response = editing
      ? await api(`/api/devices/${deviceForm.value.id}`, { method: 'PUT', body: JSON.stringify(payload) })
      : await api('/api/devices', { method: 'POST', body: JSON.stringify(payload) })
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}))
      notify(`设备保存失败：${typeof detail.detail === 'string' ? detail.detail : `HTTP ${response.status}`}`)
      return
    }
    resetDeviceEditor()
    await loadData()
    notify(editing ? '设备已更新' : '设备已添加')
  } catch (cause) {
    notify(cause instanceof Error ? cause.message : '网络异常，请确认后端已启动')
  }
}

async function editDevice(device: Device) {
  const info = deviceTypes.value.find((item) => item.key === device.type)
  const attrs: Record<string, number | string> = {}
  for (const [key, spec] of Object.entries(info?.attributes ?? {})) {
    attrs[key] = (device.attributes || {})[key] ?? spec.default
  }
  deviceForm.value = {
    id: device.id, name: device.name, room: device.room, type: device.type,
    online: device.online, active: device.active, value: device.value,
    power_consumption: device.power_consumption ?? 0, attributes: attrs,
  }
  selectedSection.value = 'devices'
}

async function deleteDevice(deviceId: number) {
  const response = await api(`/api/devices/${deviceId}`, { method: 'DELETE' })
  if (response.ok) {
    devices.value = devices.value.filter((device) => device.id !== deviceId)
    await loadData()
    notify('设备已删除')
  }
}

async function toggle(device: Device) {
  if (!device.online) return
  const response = await api(`/api/devices/${device.id}/commands`, { method: 'POST', body: JSON.stringify({ action: device.active ? 'off' : 'on' }) })
  if (!response.ok) { notify('设备控制失败'); return }
  const updated = (await response.json()).device as Device
  devices.value = devices.value.map((item) => item.id === updated.id ? updated : item)
  await loadData()
}

function resetSceneEditor() {
  sceneForm.value = { id: null, name: '', description: '', actions: [] }
  sceneActionDraft.value = { device_id: devices.value[0]?.id ? String(devices.value[0].id) : '', action: 'on' }
}

function addSceneAction() {
  if (!sceneActionDraft.value.device_id) return
  const deviceId = Number(sceneActionDraft.value.device_id)
  sceneForm.value.actions = sceneForm.value.actions.filter((item) => item.device_id !== deviceId)
  sceneForm.value.actions.push({ device_id: deviceId, action: sceneActionDraft.value.action })
}

function editScene(scene: Scene) {
  sceneForm.value = { id: scene.id, name: scene.name, description: scene.description, actions: [...scene.actions] }
  selectedSection.value = 'scenes'
  if (devices.value[0]) {
    sceneActionDraft.value = { device_id: String(devices.value[0].id), action: 'on' }
  }
}

async function saveScene() {
  const payload = { name: sceneForm.value.name.trim(), description: sceneForm.value.description.trim(), actions: sceneForm.value.actions }
  if (!payload.name || !payload.actions.length) return
  const response = sceneForm.value.id === null
    ? await api('/api/scenes', { method: 'POST', body: JSON.stringify(payload) })
    : await api(`/api/scenes/${sceneForm.value.id}`, { method: 'PUT', body: JSON.stringify(payload) })
  if (!response.ok) {
    notify('场景保存失败')
    return
  }
  const actionText = sceneForm.value.id === null ? '场景已创建' : '场景已更新'
  resetSceneEditor()
  await loadData()
  notify(actionText)
}

async function deleteScene(sceneId: number) {
  const response = await api(`/api/scenes/${sceneId}`, { method: 'DELETE' })
  if (response.ok) {
    await loadData()
    notify('场景已删除')
  }
}

async function runScene(scene: Scene) {
  const response = await api('/api/scenes/execute', { method: 'POST', body: JSON.stringify({ scene_id: scene.id }) })
  if (!response.ok) {
    notify('场景执行失败')
    return
  }
  const payload = await response.json()
  devices.value = payload.devices
  notify(`场景「${scene.name}」已执行`)
}

async function createOrder() {
  if (!orderForm.value.title.trim()) return
  const payload = {
    title: orderForm.value.title.trim(),
    description: orderForm.value.description.trim(),
    device_id: orderForm.value.device_id ? Number(orderForm.value.device_id) : null,
    priority: orderForm.value.priority,
  }
  const response = await api('/api/work-orders', { method: 'POST', body: JSON.stringify(payload) })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    notify(`报修提交失败：${typeof detail.detail === 'string' ? detail.detail : `HTTP ${response.status}`}`)
    return
  }
  orderForm.value = { title: '', description: '', device_id: '', priority: 'medium' }
  selectedSection.value = 'orders'
  await loadData()
  notify('报修已提交')
}

async function acceptOrder(orderId: number) {
  const response = await api(`/api/work-orders/${orderId}/accept`, { method: 'POST' })
  if (!response.ok) {
    notify('接单失败')
    return
  }
  selectedOrderId.value = orderId
  await loadData()
  notify('订单已接收')
}

async function updateStatus(orderId: number, status: Status) {
  const response = await api(`/api/work-orders/${orderId}/status`, { method: 'PUT', body: JSON.stringify({ status }) })
  if (!response.ok) {
    notify('状态更新失败')
    return
  }
  await loadData()
  notify('工单状态已更新')
}

async function completeOrder(orderId: number) {
  const cost = Number(completionForm.value.cost)
  const duration = Number(completionForm.value.duration)
  if (!completionForm.value.cost && !completionForm.value.duration) {
    notify('请至少填写实际费用或工时')
    return
  }
  const payload: Record<string, number> = {}
  if (completionForm.value.cost !== '') payload.cost = Number.isFinite(cost) ? cost : 0
  if (completionForm.value.duration !== '') payload.duration_minutes = Number.isFinite(duration) ? Math.round(duration) : 0
  const response = await api(`/api/work-orders/${orderId}/completion`, {
    method: 'PUT', body: JSON.stringify(payload)
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    notify(`完工登记失败：${typeof detail.detail === 'string' ? detail.detail : response.status}`)
    return
  }
  completionForm.value = { cost: '', duration: '' }
  await loadData()
  notify('完工已登记，工单已置为已解决')
}

async function selectOrder(orderId: number) {
  selectedOrderId.value = orderId
  await loadMessages(orderId)
  await loadTimeline(orderId)
}

async function sendMessage() {
  if (!activeOrder.value || !chatDraft.value.trim()) return
  const response = await api(`/api/work-orders/${activeOrder.value.id}/messages`, {
    method: 'POST',
    body: JSON.stringify({ message: chatDraft.value.trim() })
  })
  if (!response.ok) {
    notify('消息发送失败')
    return
  }
  chatDraft.value = ''
  await loadMessages(activeOrder.value.id)
  await loadData()
}

async function scrollAssistant() {
  await nextTick()
  if (assistantBox.value) assistantBox.value.scrollTop = assistantBox.value.scrollHeight
}

async function ensureSession(): Promise<number> {
  if (activeSessionId.value) return activeSessionId.value
  const response = await api('/api/assistant/sessions', { method: 'POST', body: JSON.stringify({}) })
  if (!response.ok) throw new Error('无法创建会话')
  const payload = await response.json()
  activeSessionId.value = payload.id
  await loadSessions()
  return payload.id
}

async function loadSessions() {
  const response = await api('/api/assistant/sessions')
  if (!response.ok) return
  agentSessions.value = await response.json()
}

async function openSession(id: number) {
  activeSessionId.value = id
  const response = await api(`/api/assistant/sessions/${id}`)
  if (!response.ok) return
  const payload = await response.json()
  const messages: AssistantMessage[] = (payload.messages || []).map((item: any) => ({
    role: item.role,
    content: item.content,
    citations: item.citations || [],
    proposal: item.proposal || undefined,
    intent: item.intent || undefined,
    grounded: item.grounding?.grounded,
    warnings: item.grounding?.warnings || []
  }))
  if (payload.status === 'pending_confirm') {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].proposal) { messages[index].pending = true; break }
    }
    // 报修草稿没有单独的持久化列，从 pending_action 还原到最后一个 assistant 消息上
    const pending = payload.pending_action
    if (pending?.type === 'work_order') {
      for (let index = messages.length - 1; index >= 0; index -= 1) {
        if (messages[index].role === 'assistant') {
          messages[index].workOrder = pending.payload as WorkOrderDraft
          messages[index].pending = true
          break
        }
      }
    }
  }
  assistantMessages.value = messages
  await scrollAssistant()
}

async function toggleAssistant() {
  assistantOpen.value = !assistantOpen.value
  if (!assistantOpen.value) return
  await loadSessions()
  if (!activeSessionId.value && agentSessions.value.length) {
    await openSession(agentSessions.value[0].id)
  }
  await scrollAssistant()
}

async function newChat() {
  activeSessionId.value = null
  assistantMessages.value = []
  assistantDraft.value = ''
}

async function onSessionChange(event: Event) {
  const value = Number((event.target as HTMLSelectElement).value)
  if (value) await openSession(value)
}

async function askAssistant() {
  const text = assistantDraft.value.trim()
  if (!text || assistantBusy.value) return
  assistantDraft.value = ''
  assistantBusy.value = true
  assistantMessages.value.push({ role: 'user', content: text })
  await scrollAssistant()
  try {
    const sessionId = await ensureSession()
    const response = await api(`/api/assistant/sessions/${sessionId}/messages`, {
      method: 'POST',
      body: JSON.stringify({ message: text })
    })
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}))
      throw new Error(typeof detail?.detail === 'string' ? detail.detail : 'AI 客服暂时不可用')
    }
    const payload = await response.json()
    assistantMessages.value.push({
      role: 'assistant',
      content: payload.answer || '抱歉，我没有得到有效回复。',
      citations: payload.citations || [],
      proposal: payload.proposal || undefined,
      workOrder: payload.work_order || undefined,
      intent: payload.intent || undefined,
      grounded: payload.grounded,
      warnings: payload.warnings || [],
      pending: payload.status === 'pending_confirm'
    })
    await loadSessions()
  } catch (cause) {
    assistantMessages.value.push({
      role: 'assistant',
      content: cause instanceof Error ? cause.message : '请求失败，请稍后再试。'
    })
  } finally {
    assistantBusy.value = false
    await scrollAssistant()
  }
}

async function confirmPending(message: AssistantMessage) {
  const sessionId = activeSessionId.value
  if (!sessionId || message.applied) return
  try {
    const response = await api(`/api/assistant/sessions/${sessionId}/confirm`, {
      method: 'POST', body: '{}'
    })
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}))
      const text = typeof detail.detail === 'string' ? detail.detail : `HTTP ${response.status}`
      notify(`布设失败：${text}`)
      return
    }
    const payload = await response.json()
    message.applied = true
    message.pending = false
    assistantMessages.value.push({ role: 'assistant', content: payload.message })
    await loadData()
    await loadSessions()
    notify('场景已布设')
    await scrollAssistant()
  } catch (cause) {
    notify(cause instanceof Error ? cause.message : '布设失败')
  }
}

async function cancelPending(message: AssistantMessage) {
  const sessionId = activeSessionId.value
  if (!sessionId) return
  try {
    const response = await api(`/api/assistant/sessions/${sessionId}/cancel`, {
      method: 'POST', body: '{}'
    })
    if (!response.ok) return
    const payload = await response.json()
    message.pending = false
    message.applied = true
    assistantMessages.value.push({ role: 'assistant', content: payload.message })
    await scrollAssistant()
  } catch {
    notify('取消失败')
  }
}

onMounted(() => { void loadDeviceTypes(); void hydrate() })
</script>

<template>
  <main class="shell">
    <section v-if="!user" class="auth card">
      <h1>Smart Home</h1>
      <p>智能家居运维平台</p>
      <div class="tabs">
        <button :class="{ active: mode === 'login' }" @click="mode = 'login'">登录</button>
        <button :class="{ active: mode === 'register' }" @click="mode = 'register'">注册</button>
      </div>
      <form @submit.prevent="submitAuth">
        <input v-model.trim="form.username" required placeholder="用户名">
        <input v-model="form.password" required type="password" placeholder="密码">
        <template v-if="mode === 'register'">
          <div class="role-toggle">
            <button type="button" :class="{ active: form.role === 'home_user' }" @click="form.role = 'home_user'">家庭用户</button>
            <button type="button" :class="{ active: form.role === 'repair_staff' }" @click="form.role = 'repair_staff'">维修人员</button>
          </div>
          <input v-model.trim="form.display_name" required placeholder="显示名称">
          <template v-if="form.role === 'home_user'">
            <input v-model.trim="form.home_name" required placeholder="家庭名称">
          </template>
          <template v-else>
            <input v-model.trim="form.phone" placeholder="联系电话">
            <input v-model.trim="form.specialty" placeholder="擅长领域，如 空调 / 热水器">
            <input v-model.number="form.experience_years" type="number" min="0" max="60" placeholder="从业年限">
          </template>
        </template>
        <p v-if="error" class="error">{{ error }}</p>
        <button class="primary" :disabled="busy">{{ busy ? '处理中…' : mode === 'login' ? '登录' : form.role === 'home_user' ? '创建家庭用户' : '注册维修人员' }}</button>
      </form>
      <small>测试账号：user1 / home123，维修人员：repair1 / repair123</small>
    </section>

    <template v-else>
      <div class="app-shell">
        <aside class="sidebar">
          <div class="brand">
            <div class="brand-mark">S</div>
            <div>
              <strong>smarthome</strong>
              <span>OPS PLATFORM</span>
            </div>
          </div>
          <div class="workspace-switcher">
            <div class="workspace-avatar">栖</div>
            <div>
              <small>当前空间</small>
              <strong>{{ isHome ? '我的家庭空间' : '维修服务中心' }}</strong>
            </div>
          </div>
          <nav class="nav-list">
            <p>工作台</p>
            <button class="nav-item" :class="{ selected: selectedSection === 'overview' }" @click="selectedSection = 'overview'">◈ <span>{{ isHome ? '家庭总览' : '工单中心' }}</span></button>
            <button v-if="isHome" class="nav-item" :class="{ selected: selectedSection === 'devices' }" @click="selectedSection = 'devices'">▣ <span>设备管理</span><em>{{ devices.length }}</em></button>
            <button v-if="isHome" class="nav-item" :class="{ selected: selectedSection === 'scenes' }" @click="selectedSection = 'scenes'">✧ <span>自动化场景</span></button>
            <button v-if="isHome" class="nav-item" :class="{ selected: selectedSection === 'stats' }" @click="openStats()">▤ <span>数据统计</span></button>
            <button class="nav-item" :class="{ selected: selectedSection === 'orders' }" @click="selectedSection = 'orders'">⌁ <span>维修工单</span><em>{{ pending }}</em></button>
            <template v-if="isHome">
              <p>智能助手</p>
              <button class="nav-item assistant-nav" @click="toggleAssistant()">✦ <span>AI 管家</span><em>看方案</em></button>
            </template>
          </nav>
          <div class="sidebar-footer">
            <div class="user-card">
              <div class="user-avatar">{{ user.display_name.slice(0, 1) }}</div>
              <div>
                <strong>{{ user.display_name }}</strong>
                <span>{{ isHome ? '家居用户' : '维修人员' }}</span>
              </div>
            </div>
          </div>
        </aside>

        <main class="main-content">
          <header>
            <div>
              <span class="eyebrow">{{ isHome ? 'HOME OVERVIEW' : 'SERVICE OPERATIONS' }}</span>
              <b>{{ isHome ? '今天的家，一切如常。' : '工单服务，清晰高效。' }}</b>
              <small>{{ user.username }} · {{ isHome ? '家居用户' : '维修人员' }}</small>
            </div>
            <button @click="logout">退出</button>
          </header>

          <section v-if="isHome && selectedSection === 'overview'" class="content">
            <div class="stats">
              <div><span class="stat-label">在线设备</span><b>{{ onlineCount }}<em>/ {{ devices.length }}</em></b></div>
              <div><span class="stat-label">自动化场景</span><b>{{ scenes.length }}</b></div>
              <div><span class="stat-label">待处理工单</span><b>{{ pending }}</b></div>
            </div>

            <article class="card panel-card">
              <div class="panel-head">
                <h2>我的设备</h2>
                <span class="panel-hint">{{ onlineCount }} 台在线 · 共 {{ devices.length }} 台</span>
              </div>
              <div v-if="!devices.length" class="empty">还没有设备，去「设备管理」添加</div>
              <div v-else class="device-grid">
                <div v-for="device in devices" :key="device.id"
                     :class="['device-card', { on: device.active && device.online, offline: !device.online }]">
                  <div class="device-card-head">
                    <span class="device-icon" :class="device.type">{{ deviceIcon(device.type) }}</span>
                    <span :class="['device-state', device.online ? 'online' : 'offline',
                                   { running: device.active && device.online }]">
                      {{ device.online ? (device.active ? '运行中' : '已关闭') : '离线' }}
                    </span>
                  </div>
                  <b class="device-name">{{ device.name }}</b>
                  <small class="device-meta">{{ device.room }} · {{ typeLabel(device.type) }}</small>
                  <div class="device-value">{{ device.value }}</div>
                  <div class="device-card-foot">
                    <span class="device-power">{{ device.power_consumption }}W</span>
                    <button class="device-toggle" :class="{ active: device.active }"
                            :disabled="!device.online" @click="toggle(device)">
                      {{ device.active ? '关闭' : '开启' }}
                    </button>
                  </div>
                </div>
              </div>
            </article>

            <article class="card panel-card">
              <div class="panel-head">
                <h2>自动化场景</h2>
                <span class="panel-hint">点击卡片立即执行</span>
              </div>
              <div v-if="!scenes.length" class="empty">还没有场景，去「自动化场景」创建</div>
              <div v-else class="scene-grid">
                <button v-for="scene in scenes" :key="scene.id" class="scene-card" @click="runScene(scene)">
                  <span class="scene-icon">✦</span>
                  <b>{{ scene.name }}</b>
                  <small>{{ scene.description }}</small>
                  <em>{{ scene.actions.length }} 个动作</em>
                </button>
              </div>
            </article>
            <div class="columns">
              <article class="card panel-card">
                <h2>我的报修</h2>
                <div v-for="order in orders" :key="order.id" class="item" :class="{ selected: selectedOrderId === order.id }" @click="selectOrder(order.id)">
                  <span><b>#{{ order.id }} {{ order.title }}</b><small>{{ order.description }}</small></span>
                  <em><span :class="['priority-chip', order.priority]">{{ priorityText[order.priority] }}</span>{{ statusText[order.status] }}</em>
                </div>
              </article>
              <article class="card panel-card">
                <h2>新建报修</h2>
                <form @submit.prevent="createOrder">
                  <select v-model="orderForm.device_id">
                    <option value="">不关联设备</option>
                    <option v-for="device in devices" :key="device.id" :value="String(device.id)">
                      {{ device.name }}（{{ device.room }}）
                    </option>
                  </select>
                  <input v-model="orderForm.title" required placeholder="问题标题">
                  <textarea v-model="orderForm.description" placeholder="问题描述"></textarea>
                  <select v-model="orderForm.priority">
                    <option value="low">低优先级</option>
                    <option value="medium">普通</option>
                    <option value="high">高优先级</option>
                    <option value="urgent">紧急</option>
                  </select>
                  <button class="primary">提交报修</button>
                </form>
              </article>
            </div>

            <p class="footer-strip">状态同步：{{ orders.length ? '已连接' : '等待更新' }}</p>
          </section>

          <section v-if="isHome && selectedSection === 'devices'" class="content">
            <div class="panel-toolbar">
              <h2>设备管理</h2>
              <button class="primary" @click="resetDeviceEditor">新增设备</button>
            </div>
            <div class="manager-grid">
              <article class="card custom-panel">
                <h3>{{ deviceForm.id === null ? '添加设备' : '编辑设备' }}</h3>
                <form @submit.prevent="saveDevice">
                  <label class="field">
                    <span>设备名称</span>
                    <input v-model="deviceForm.name" required placeholder="如 客厅中央空调">
                  </label>

                  <div class="form-row">
                    <label class="field">
                      <span>设备类型</span>
                      <select v-model="deviceForm.type" required @change="applyTypeDefaults">
                        <option v-for="item in deviceTypes" :key="item.key" :value="item.key">{{ item.label }}</option>
                      </select>
                    </label>
                    <label class="field">
                      <span>所在房间</span>
                      <input v-model="deviceForm.room" list="room-options" required placeholder="如 客厅">
                    </label>
                  </div>
                  <datalist id="room-options">
                    <option v-for="room in roomOptions" :key="room" :value="room" />
                  </datalist>

                  <div class="form-row">
                    <label class="field">
                      <span>状态值{{ currentTypeInfo?.status_from_attributes ? '（由参数自动生成）' : '' }}</span>
                      <input v-model="deviceForm.value" :disabled="!!currentTypeInfo?.status_from_attributes"
                             placeholder="如 42 AQI">
                    </label>
                    <label class="field">
                      <span>额定功耗 (W)</span>
                      <input v-model.number="deviceForm.power_consumption" type="number" min="0" step="1" placeholder="如 1500">
                    </label>
                  </div>

                  <div v-if="attributeFields.length" class="attr-grid">
                    <label v-for="field in attributeFields" :key="field.key" class="attr-field">
                      <span>{{ field.spec.label }}</span>
                      <select v-if="field.spec.type === 'enum'" v-model="deviceForm.attributes[field.key]">
                        <option v-for="choice in field.spec.choices" :key="choice" :value="choice">
                          {{ enumLabels[field.key]?.[choice] ?? choice }}
                        </option>
                      </select>
                      <input v-else v-model.number="deviceForm.attributes[field.key]" type="number"
                             :min="field.spec.min" :max="field.spec.max">
                    </label>
                  </div>

                  <div class="form-row checks">
                    <label class="inline-check"><input v-model="deviceForm.online" type="checkbox"> 在线</label>
                    <label class="inline-check"><input v-model="deviceForm.active" type="checkbox"> 当前开启</label>
                  </div>

                  <button class="primary">{{ deviceForm.id === null ? '保存设备' : '更新设备' }}</button>
                </form>
              </article>
              <article class="card custom-panel">
                <h3>设备列表</h3>
                <div v-if="!devices.length" class="empty">暂无设备</div>
                <div v-for="device in devices" :key="device.id" class="detail-item">
                  <div>
                    <b>{{ device.name }}<span v-if="!device.online" class="offline-tag">离线</span></b>
                    <small>{{ device.room }} · {{ typeLabel(device.type) }} · {{ device.power_consumption }}W</small>
                    <small v-if="attributeSummary(device)" class="attr-summary">{{ attributeSummary(device) }}</small>
                  </div>
                  <div class="row-actions">
                    <button class="ghost" @click="editDevice(device)">编辑</button>
                    <button class="ghost danger" @click="deleteDevice(device.id)">删除</button>
                  </div>
                </div>
              </article>
            </div>
          </section>

          <section v-if="isHome && selectedSection === 'scenes'" class="content">
            <div class="panel-toolbar">
              <h2>自动化场景</h2>
              <button class="primary" @click="resetSceneEditor">新建场景</button>
            </div>
            <div class="manager-grid">
              <article class="card custom-panel">
                <h3>{{ sceneForm.id === null ? '创建场景' : '编辑场景' }}</h3>
                <form @submit.prevent="saveScene">
                  <input v-model="sceneForm.name" required placeholder="场景名称">
                  <textarea v-model="sceneForm.description" placeholder="场景说明"></textarea>
                  <div class="scene-builder">
                    <select v-model="sceneActionDraft.device_id">
                      <option value="">选择设备</option>
                      <option v-for="device in devices" :key="device.id" :value="String(device.id)">{{ device.name }}</option>
                    </select>
                    <select v-model="sceneActionDraft.action">
                      <option value="on">开启</option>
                      <option value="off">关闭</option>
                    </select>
                    <button type="button" class="ghost" @click="addSceneAction">添加动作</button>
                  </div>
                  <div v-if="sceneForm.actions.length" class="scene-actions">
                    <div v-for="(action, index) in sceneForm.actions" :key="`${action.device_id}-${action.action}`" class="chip-row">
                      <span>{{ devices.find((device) => device.id === action.device_id)?.name || '未知设备' }}</span>
                      <em>{{ action.action === 'on' ? '开启' : '关闭' }}</em>
                      <button type="button" class="chip-delete" @click="sceneForm.actions.splice(index, 1)">删除</button>
                    </div>
                  </div>
                  <button class="primary">{{ sceneForm.id === null ? '保存场景' : '更新场景' }}</button>
                </form>
              </article>
              <article class="card custom-panel">
                <h3>场景列表</h3>
                <div v-if="!scenes.length" class="empty">暂无场景</div>
                <div v-for="scene in scenes" :key="scene.id" class="detail-item scene-item">
                  <div>
                    <b>{{ scene.name }}</b>
                    <small>{{ scene.description }}</small>
                  </div>
                  <div class="row-actions">
                    <button class="ghost" @click="runScene(scene)">执行</button>
                    <button class="ghost" @click="editScene(scene)">编辑</button>
                    <button class="ghost danger" @click="deleteScene(scene.id)">删除</button>
                  </div>
                </div>
              </article>
            </div>
          </section>

          <section v-if="isHome && selectedSection === 'stats'" class="content">
            <div class="panel-toolbar">
              <h2>数据统计</h2>
              <button class="ghost" @click="loadStats()">刷新</button>
            </div>
            <div v-if="!stats" class="empty">加载中…</div>
            <template v-else>
              <div class="stats">
                <div>设备总数 <b>{{ stats.device_stats.device_total }}</b></div>
                <div>在线设备 <b>{{ stats.device_stats.device_online }}</b></div>
                <div>当前功耗 <b>{{ powerActiveWatts }} W</b></div>
              </div>

              <div class="columns">
                <article class="card panel-card">
                  <h2>设备偏多的房间</h2>
                  <p class="hint">嵌套子查询 · 房间设备数高于全屋房间均值</p>
                  <div v-if="!stats.rooms_above_average.length" class="empty">没有高于平均的房间</div>
                  <div v-for="row in stats.rooms_above_average" :key="row.room" class="item">
                    <span><b>{{ row.room }}</b><small>高于全屋房间均值</small></span>
                    <em>{{ row.device_count }} 台</em>
                  </div>
                </article>

                <article class="card panel-card">
                  <h2>动作最多的场景</h2>
                  <p class="hint">聚合排序 · 按动作数量取 Top 1</p>
                  <div v-if="!stats.top_scene" class="empty">暂无场景</div>
                  <div v-else class="item">
                    <span><b>{{ stats.top_scene.name }}</b><small>{{ stats.top_scene.description }}</small></span>
                    <em>{{ stats.top_scene.action_count }} 个动作</em>
                  </div>
                </article>
              </div>

              <article class="card panel-card">
                <h2>同房间内的多类型设备</h2>
                <p class="hint">自连接 · devices 表与自身按房间关联，用 type 排序去重</p>
                <div v-if="!stats.cross_type_rooms.length" class="empty">暂无</div>
                <div v-for="(row, index) in stats.cross_type_rooms" :key="index" class="item">
                  <span>
                    <b>{{ row.room }}</b>
                    <small>{{ row.device_a }}（{{ row.type_a }}）× {{ row.device_b }}（{{ row.type_b }}）</small>
                  </span>
                </div>
              </article>

              <p class="hint source">
                汇总数据来源：{{ stats.view_source === 'database_view' ? '数据库视图 v_home_device_stats' : 'ORM 降级查询（SQLite）' }}
              </p>
            </template>
          </section>

          <section v-if="selectedSection === 'orders'" class="content">
            <div v-if="isHome" class="panel-toolbar">
              <h2>维修工单</h2>
            </div>
            <div v-if="!isHome" class="panel-toolbar">
              <h2>工单中心</h2>
            </div>
            <div v-if="!isHome && technician" class="tech-bar">
              <div class="tech-card"><small>我的工单</small><b>{{ technician.stats.total }}</b></div>
              <div class="tech-card"><small>处理中</small><b>{{ technician.stats.in_progress }}</b></div>
              <div class="tech-card"><small>已解决</small><b>{{ technician.stats.resolved }}</b></div>
              <div class="tech-card"><small>可接单</small><b>{{ technician.available_orders }}</b></div>
              <div class="tech-profile">
                <b>{{ technician.specialty || '未填写专长' }}</b>
                <small>评分 {{ technician.rating ?? '-' }} · 从业 {{ technician.experience_years ?? '-' }} 年 · {{ technician.phone || '无联系电话' }}</small>
              </div>
            </div>

            <div class="orders-grid">
              <article class="card custom-panel order-list">
                <h3>{{ isHome ? '我的报修' : '待处理工单' }}</h3>
                <div v-if="!orders.length" class="empty">暂无工单</div>
                <div v-for="order in orders" :key="order.id" class="order-card" :class="{ active: selectedOrderId === order.id }" @click="selectOrder(order.id)">
                  <div class="order-meta">
                    <b>#{{ order.id }} {{ order.title }}</b>
                    <span>{{ statusText[order.status] }}</span>
                  </div>
                  <small v-if="order.device_name">关联设备：{{ order.device_name }}</small>
                  <small>{{ order.description }}</small>
                  <div class="order-footer">
                    <em>
                      <span :class="['priority-chip', order.priority]">{{ priorityText[order.priority] }}</span>
                      {{ order.message_count }} 条消息
                    </em>
                    <button v-if="!isHome && (order.status === 'open' || order.status === 'assigned') && !order.assigned_to" type="button" class="ghost" @click.stop="acceptOrder(order.id)">接单</button>
                  </div>
                </div>
              </article>

              <article v-if="activeOrder" class="card custom-panel chat-panel">
                <div class="chat-header">
                  <div>
                    <h3>#{{ activeOrder.id }} {{ activeOrder.title }}</h3>
                    <small>{{ statusText[activeOrder.status] }}</small>
                  </div>
                  <div v-if="!isHome" class="chat-actions">
                    <select :value="activeOrder.status" @change="updateStatus(activeOrder.id, ($event.target as HTMLSelectElement).value as Status)">
                      <option v-for="(label, value) in statusText" :key="value" :value="value">{{ label }}</option>
                    </select>
                  </div>
                </div>

                <div v-if="!isHome && activeOrder.assigned_to === user.id && activeOrder.status !== 'resolved'"
                     class="completion-bar">
                  <span class="completion-label">完工登记</span>
                  <input v-model="completionForm.cost" type="number" min="0" step="1" placeholder="实际费用(元)">
                  <input v-model="completionForm.duration" type="number" min="0" step="5" placeholder="工时(分钟)">
                  <button class="ghost" @click="completeOrder(activeOrder.id)">提交完工</button>
                </div>
                <p v-else-if="!isHome && activeOrder.status === 'resolved' && activeOrder.cost !== null"
                   class="hint completion-done">
                  完工记录：费用 {{ activeOrder.cost }} 元 · 工时 {{ activeOrder.duration_minutes ?? '-' }} 分钟
                </p>

                <div v-if="timeline.length" class="timeline">
                  <p class="hint">状态流转 · 由数据库触发器记录</p>
                  <div v-for="(event, index) in timeline" :key="index" class="timeline-row">
                    <span>{{ statusText[event.from] }}</span>
                    <em>→</em>
                    <span class="to">{{ statusText[event.to] }}</span>
                    <small>{{ new Date(event.at).toLocaleString('zh-CN') }}</small>
                  </div>
                </div>

                <div v-if="!isHome && activeOrder.assigned_to !== user.id" class="empty">当前工单尚未指派给您，需先接单后才能沟通。</div>
                <template v-else>
                  <div class="chat-box">
                    <div v-if="!(orderMessages[activeOrder.id] || []).length" class="empty small">暂无消息，先发起沟通。</div>
                    <div v-for="item in orderMessages[activeOrder.id] || []" :key="item.id" :class="['chat-message', { self: item.sender_id === user.id }]">
                      <span>{{ item.message }}</span>
                    </div>
                  </div>
                  <div class="chat-input">
                    <textarea v-model="chatDraft" rows="3" placeholder="输入消息..."></textarea>
                    <button class="primary" @click="sendMessage">发送</button>
                  </div>
                </template>
              </article>
            </div>
          </section>
        </main>
      </div>
    </template>

    <template v-if="user && isHome">
      <button class="assistant-fab" @click="toggleAssistant()">
        <span class="fab-icon">✦</span>
        <span>AI 管家</span>
      </button>

      <aside v-if="assistantOpen" class="assistant-drawer">
        <div class="assistant-head">
          <div>
            <b>AI 客服</b>
            <small>查手册 · 查设备 · 帮您报修</small>
          </div>
          <button @click="assistantOpen = false">收起</button>
        </div>

        <div class="session-bar">
          <select :value="activeSessionId ?? ''" @change="onSessionChange">
            <option value="" disabled>选择对话…</option>
            <option v-for="item in agentSessions" :key="item.id" :value="item.id">
              {{ item.title }}<template v-if="item.status === 'pending_confirm'"> · 待确认</template>
            </option>
          </select>
          <button class="ghost" @click="newChat">新对话</button>
        </div>

        <div ref="assistantBox" class="assistant-body">
          <div v-if="!assistantMessages.length" class="assistant-empty">
            <p>您好，我是智家管家。可以试试问我：</p>
            <button v-for="item in suggestions" :key="item" class="assistant-suggest" @click="assistantDraft = item">
              {{ item }}
            </button>
          </div>

          <div v-for="(item, index) in assistantMessages" :key="index" :class="['assistant-msg', item.role]">
            <div class="bubble">{{ item.content }}</div>
            <div v-if="item.citations && item.citations.length" class="citations">
              <span v-for="cite in item.citations" :key="cite.source + cite.section">
                📄 {{ cite.source }}<template v-if="cite.section"> · {{ cite.section }}</template>
              </span>
            </div>

            <div v-if="item.proposal" class="proposal">
              <div class="proposal-head">
                <b>{{ item.proposal.name }}</b>
                <span>{{ item.proposal.actions.length }} 个动作</span>
              </div>
              <p v-if="item.proposal.description" class="proposal-desc">{{ item.proposal.description }}</p>
              <div class="proposal-actions">
                <div v-for="(act, i) in item.proposal.actions" :key="i" class="proposal-action">
                  <span>{{ act.device_name }}<em class="room">{{ act.room }}</em></span>
                  <em>{{ act.action === 'off' ? '关闭' : (act.label || '开启') }}</em>
                </div>
              </div>
              <p v-if="item.proposal.rationale" class="proposal-why">{{ item.proposal.rationale }}</p>
              <div v-if="item.proposal.missing.length" class="proposal-missing">
                未匹配到：{{ item.proposal.missing.join('、') }}
              </div>
              <p v-if="item.applied" class="proposal-done">✓ 已布设为场景，可在「自动化场景」里执行</p>
              <div v-else-if="item.pending" class="proposal-buttons">
                <button class="primary" @click="confirmPending(item)">确认布设</button>
                <button class="ghost" @click="cancelPending(item)">取消</button>
              </div>
            </div>

            <div v-if="item.workOrder" class="proposal work-order-card">
              <div class="proposal-head">
                <b>报修草稿 · {{ item.workOrder.title }}</b>
                <span>{{ priorityText[item.workOrder.priority] || item.workOrder.priority }}</span>
              </div>
              <p class="proposal-desc">信息由 AI 整理，提交前请核对一遍</p>
              <pre class="work-order-desc">{{ item.workOrder.description }}</pre>
              <div v-if="item.workOrder.device" class="device-snap">
                设备快照：{{ item.workOrder.device.name }} ·
                {{ item.workOrder.device.online ? '在线' : '离线' }} ·
                {{ item.workOrder.device.value }}
              </div>
              <p v-if="item.applied" class="proposal-done">✓ 已提交报修，可在「维修工单」里跟进</p>
              <div v-else-if="item.pending" class="proposal-buttons">
                <button class="primary" @click="confirmPending(item)">提交报修</button>
                <button class="ghost" @click="cancelPending(item)">取消</button>
              </div>
            </div>

            <div v-if="item.role === 'assistant' && item.intent" class="msg-meta">
              <span class="intent-chip">{{ item.intent }}</span>
              <span v-if="item.grounded === false" class="ground-warn">
                有 {{ (item.warnings || []).length }} 处未溯源
              </span>
            </div>
          </div>

          <div v-if="assistantBusy" class="assistant-msg assistant">
            <div class="bubble thinking">正在查资料…</div>
          </div>
        </div>

        <div class="assistant-input">
          <textarea v-model="assistantDraft" rows="2" placeholder="描述您遇到的问题，回车发送…"
                    @keydown.enter.exact.prevent="askAssistant"></textarea>
          <button class="primary" :disabled="assistantBusy || !assistantDraft.trim()" @click="askAssistant">发送</button>
        </div>
      </aside>
    </template>

    <div v-if="message" class="toast">{{ message }}</div>
  </main>
</template>
