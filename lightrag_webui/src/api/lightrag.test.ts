import { afterEach, beforeAll, describe, expect, test } from 'bun:test'

type DocumentsRequest = {
  status_filter?: 'pending' | 'processing' | 'preprocessed' | 'processed' | 'failed' | null
  page: number
  page_size: number
  sort_field: 'created_at' | 'updated_at' | 'id' | 'file_path'
  sort_direction: 'asc' | 'desc'
}

type LightragApiModule = typeof import('./lightrag')

const storageMock = () => {
  const data = new Map<string, string>()

  return {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => {
      data.set(key, value)
    },
    removeItem: (key: string) => {
      data.delete(key)
    },
    clear: () => {
      data.clear()
    }
  }
}

let apiModule: LightragApiModule
const realXMLHttpRequest = globalThis.XMLHttpRequest

beforeAll(async () => {
  Object.defineProperty(globalThis, 'localStorage', {
    value: storageMock(),
    configurable: true
  })
  Object.defineProperty(globalThis, 'sessionStorage', {
    value: storageMock(),
    configurable: true
  })

  apiModule = await import('./lightrag')
})

afterEach(() => {
  apiModule.__setAxiosAdapterForTests(undefined)
  apiModule.__resetPaginatedDocumentRequestsForTests()
  globalThis.XMLHttpRequest = realXMLHttpRequest
})

type CapturedObjectPut = {
  method: string
  url: string
  headers: Record<string, string>
  body: unknown
}

const parseAxiosData = (data: unknown): unknown => {
  if (typeof data !== 'string') return data
  return JSON.parse(data)
}

const installSuccessfulObjectPut = (status = 204): CapturedObjectPut[] => {
  const puts: CapturedObjectPut[] = []

  class FakeXMLHttpRequest {
    upload: { onprogress: ((event: ProgressEvent) => void) | null } = {
      onprogress: null
    }

    status = status
    responseText = ''
    onload: (() => void) | null = null
    onerror: (() => void) | null = null
    onabort: (() => void) | null = null

    private method = ''
    private url = ''
    private headers: Record<string, string> = {}

    open(method: string, url: string) {
      this.method = method
      this.url = url
    }

    setRequestHeader(name: string, value: string) {
      this.headers[name] = value
    }

    send(body?: unknown) {
      puts.push({
        method: this.method,
        url: this.url,
        headers: { ...this.headers },
        body
      })
      this.upload.onprogress?.({
        lengthComputable: true,
        loaded: 11,
        total: 11
      } as ProgressEvent)
      this.onload?.()
    }
  }

  globalThis.XMLHttpRequest = FakeXMLHttpRequest as unknown as typeof XMLHttpRequest
  return puts
}

describe('getDocumentsPaginated', () => {
  test('issues a fresh request after aborting a timed-out in-flight request', async () => {
    const request: DocumentsRequest = {
      status_filter: null,
      page: 1,
      page_size: 20,
      sort_field: 'updated_at',
      sort_direction: 'desc'
    }

    let callCount = 0
    const resolvers: Array<(value: any) => void> = []

    apiModule.__setPaginatedDocumentsPostForTests((_request, controller) => {
      callCount += 1

      return new Promise((resolve, reject) => {
        resolvers.push(resolve)
        controller.signal.addEventListener(
          'abort',
          () => reject(new DOMException('Aborted', 'AbortError')),
          { once: true }
        )
      })
    })

    const firstRequest = apiModule.getDocumentsPaginated(request)
    const secondRequest = apiModule.getDocumentsPaginated(request)

    expect(callCount).toBe(1)

    apiModule.abortDocumentsPaginated(request)
    const [firstResult, secondResult] = await Promise.allSettled([
      firstRequest,
      secondRequest
    ])
    expect(firstResult.status).toBe('rejected')
    expect(secondResult.status).toBe('rejected')

    const thirdRequest = apiModule.getDocumentsPaginated(request)
    expect(callCount).toBe(2)

    resolvers[1]({
      documents: [],
      pagination: {
        page: 1,
        page_size: 20,
        total_count: 0,
        total_pages: 0,
        has_next: false,
        has_prev: false
      },
      status_counts: { all: 0 }
    })

    await expect(thirdRequest).resolves.toEqual({
      documents: [],
      pagination: {
        page: 1,
        page_size: 20,
        total_count: 0,
        total_pages: 0,
        has_next: false,
        has_prev: false
      },
      status_counts: { all: 0 }
    })
  })

  test('times out hanging requests and allows a fresh retry', async () => {
    const request: DocumentsRequest = {
      status_filter: null,
      page: 1,
      page_size: 20,
      sort_field: 'updated_at',
      sort_direction: 'desc'
    }

    let callCount = 0
    const resolvers: Array<(value: any) => void> = []

    apiModule.__setPaginatedDocumentsPostForTests((_request, controller) => {
      callCount += 1

      return new Promise((resolve, reject) => {
        resolvers.push(resolve)
        controller.signal.addEventListener(
          'abort',
          () => reject(new DOMException('Aborted', 'AbortError')),
          { once: true }
        )
      })
    })

    await expect(
      apiModule.getDocumentsPaginatedWithTimeout(request, 1)
    ).rejects.toThrow('Document fetch timeout')

    expect(callCount).toBe(1)

    const retryRequest = apiModule.getDocumentsPaginated(request)
    expect(callCount).toBe(2)

    resolvers[1]({
      documents: [],
      pagination: {
        page: 1,
        page_size: 20,
        total_count: 0,
        total_pages: 0,
        has_next: false,
        has_prev: false
      },
      status_counts: { all: 0 }
    })

    await expect(retryRequest).resolves.toEqual({
      documents: [],
      pagination: {
        page: 1,
        page_size: 20,
        total_count: 0,
        total_pages: 0,
        has_next: false,
        has_prev: false
      },
      status_counts: { all: 0 }
    })
  })

  test('does not abort a shared request when only one timeout subscriber expires', async () => {
    const request: DocumentsRequest = {
      status_filter: null,
      page: 1,
      page_size: 20,
      sort_field: 'updated_at',
      sort_direction: 'desc'
    }

    let callCount = 0
    let resolveSharedRequest: ((value: any) => void) | undefined
    let abortCount = 0

    apiModule.__setPaginatedDocumentsPostForTests((_request, controller) => {
      callCount += 1

      return new Promise((resolve, reject) => {
        resolveSharedRequest = resolve
        controller.signal.addEventListener(
          'abort',
          () => {
            abortCount += 1
            reject(new DOMException('Aborted', 'AbortError'))
          },
          { once: true }
        )
      })
    })

    const shortTimeoutRequest = apiModule.getDocumentsPaginatedWithTimeout(request, 1)
    const longTimeoutRequest = apiModule.getDocumentsPaginatedWithTimeout(request, 100)

    await expect(shortTimeoutRequest).rejects.toThrow('Document fetch timeout')

    expect(callCount).toBe(1)
    expect(abortCount).toBe(0)

    resolveSharedRequest?.({
      documents: [],
      pagination: {
        page: 1,
        page_size: 20,
        total_count: 0,
        total_pages: 0,
        has_next: false,
        has_prev: false
      },
      status_counts: { all: 0 }
    })

    await expect(longTimeoutRequest).resolves.toEqual({
      documents: [],
      pagination: {
        page: 1,
        page_size: 20,
        total_count: 0,
        total_pages: 0,
        has_next: false,
        has_prev: false
      },
      status_counts: { all: 0 }
    })
  })
})

describe('isUserAbortError', () => {
  // Regression: the Stop button must suppress query cancellation everywhere it
  // surfaces — both the main stream catch and the guest-token retry catch (which
  // otherwise redirects an aborting guest to the login page). Both sites share
  // this predicate, so locking down its behavior guards both fixes.
  test('treats an aborted signal as a user abort regardless of the error', () => {
    const controller = new AbortController()
    controller.abort()
    expect(apiModule.isUserAbortError(controller.signal, new Error('boom'))).toBe(true)
  })

  test('treats an AbortError as a user abort even when the signal is absent', () => {
    const abortError = new DOMException('Aborted', 'AbortError')
    expect(apiModule.isUserAbortError(undefined, abortError)).toBe(true)
  })

  test('does not treat a real failure on a live signal as a user abort', () => {
    const controller = new AbortController()
    expect(apiModule.isUserAbortError(controller.signal, new Error('network down'))).toBe(false)
    expect(apiModule.isUserAbortError(undefined, new Error('network down'))).toBe(false)
  })
})

describe('response interceptor', () => {
  test('an HTTP failure carries its status, not just a formatted message', async () => {
    // The interceptor rewrites every non-401 AxiosError into a plain Error.
    // Without the status as a property, callers that branch on it (the
    // document list treats a 4xx as permanent and must not let it trip the
    // refresh circuit breaker) see only `undefined` and fall back to
    // "unknown, retry".
    apiModule.__setAxiosAdapterForTests(async () => {
      throw {
        response: {
          status: 404,
          statusText: 'Not Found',
          data: { detail: 'no such workspace' }
        },
        config: { url: '/documents/pipeline_status' }
      }
    })

    try {
      const failure = await apiModule
        .getPipelineStatus()
        .then(() => null)
        .catch((error: unknown) => error as Error & { status?: number })

      expect(failure).toBeInstanceOf(Error)
      expect(failure?.status).toBe(404)
      expect(failure?.message).toContain('404 Not Found')
      expect(failure?.message).toContain('no such workspace')
    } finally {
      apiModule.__setAxiosAdapterForTests(undefined)
    }
  })

  test('toHttpRequestError builds the same shape directly', () => {
    const error = apiModule.toHttpRequestError(422, 'Unprocessable Entity', { detail: 'bad' }, '/documents/paginated')

    expect(error.status).toBe(422)
    expect(error.message).toContain('422 Unprocessable Entity')
    expect(error.message).toContain('/documents/paginated')
  })
})

describe('uploadDocument', () => {
  test('uses object-store presign, direct PUT, and complete instead of multipart upload', async () => {
    const objectPuts = installSuccessfulObjectPut()
    const backendRequests: Array<{ url?: string; method?: string; data: unknown }> = []
    const uploadUrl = 'https://objects.example.com/docs/report.pdf?signature=abc'
    const objectKey = 'lightrag/uploads/tenant_a/upload_1/report.pdf'

    apiModule.__setAxiosAdapterForTests(async (config: any) => {
      backendRequests.push({
        url: config.url,
        method: config.method,
        data: parseAxiosData(config.data)
      })

      if (config.url === '/documents/uploads/presign') {
        return {
          data: {
            upload_id: 'upload_1',
            object_key: objectKey,
            upload_url: uploadUrl,
            method: 'PUT',
            headers: {
              'Content-Type': 'application/pdf',
              'x-amz-meta-size': '11'
            },
            expires_in: 900,
            expires_at: '2026-09-16T00:15:00+00:00',
            max_size: 104857600
          },
          status: 200,
          statusText: 'OK',
          headers: { 'content-type': 'application/json' },
          config
        }
      }

      if (config.url === '/documents/uploads/complete') {
        return {
          data: {
            status: 'success',
            message: 'Object uploaded successfully.',
            track_id: 'track-1'
          },
          status: 200,
          statusText: 'OK',
          headers: { 'content-type': 'application/json' },
          config
        }
      }

      throw new Error(`unexpected backend request: ${config.url}`)
    })

    const progress: number[] = []
    const file = new File(['hello world'], 'report.pdf', { type: 'application/pdf' })

    const result = await apiModule.uploadDocument(file, (percent) => {
      progress.push(percent)
    })

    expect(result.track_id).toBe('track-1')
    expect(backendRequests.map((request) => request.url)).toEqual([
      '/documents/uploads/presign',
      '/documents/uploads/complete'
    ])
    expect(backendRequests[0].data).toEqual({
      filename: 'report.pdf',
      content_type: 'application/pdf',
      size: 11
    })
    expect(backendRequests[1].data).toEqual({
      upload_id: 'upload_1',
      object_key: objectKey
    })
    expect(objectPuts).toHaveLength(1)
    expect(objectPuts[0]).toEqual({
      method: 'PUT',
      url: uploadUrl,
      headers: {
        'Content-Type': 'application/pdf',
        'x-amz-meta-size': '11'
      },
      body: file
    })
    expect(progress.includes(100)).toBe(true)
  })

  test('falls back to local multipart upload only when object ingestion is not configured', async () => {
    const objectPuts = installSuccessfulObjectPut()
    const backendRequests: Array<{ url?: string; data: unknown }> = []

    apiModule.__setAxiosAdapterForTests(async (config: any) => {
      backendRequests.push({ url: config.url, data: config.data })

      if (config.url === '/documents/uploads/presign') {
        throw {
          response: {
            status: 503,
            statusText: 'Service Unavailable',
            data: { detail: 'Object-store document ingestion is not configured.' }
          },
          config
        }
      }

      if (config.url === '/documents/upload') {
        return {
          data: {
            status: 'success',
            message: 'File uploaded successfully.',
            track_id: 'legacy-track'
          },
          status: 200,
          statusText: 'OK',
          headers: { 'content-type': 'application/json' },
          config
        }
      }

      throw new Error(`unexpected backend request: ${config.url}`)
    })

    const file = new File(['hello world'], 'report.pdf', { type: 'application/pdf' })

    const result = await apiModule.uploadDocument(file)

    expect(result.track_id).toBe('legacy-track')
    expect(backendRequests.map((request) => request.url)).toEqual([
      '/documents/uploads/presign',
      '/documents/upload'
    ])
    expect(backendRequests[1].data).toBeInstanceOf(FormData)
    expect(objectPuts).toHaveLength(0)
  })

  test('does not hide presign service failures behind local multipart upload', async () => {
    const objectPuts = installSuccessfulObjectPut()
    const backendRequests: Array<{ url?: string }> = []

    apiModule.__setAxiosAdapterForTests(async (config: any) => {
      backendRequests.push({ url: config.url })

      if (config.url === '/documents/uploads/presign') {
        throw {
          response: {
            status: 503,
            statusText: 'Service Unavailable',
            data: {
              detail: {
                error: 'CoordinationUnavailableError',
                message: 'Coordination transaction failed'
              }
            }
          },
          config
        }
      }

      throw new Error(`unexpected backend request: ${config.url}`)
    })

    const file = new File(['hello world'], 'report.pdf', { type: 'application/pdf' })

    await expect(apiModule.uploadDocument(file)).rejects.toThrow(
      'Coordination transaction failed'
    )
    expect(backendRequests.map((request) => request.url)).toEqual([
      '/documents/uploads/presign'
    ])
    expect(objectPuts).toHaveLength(0)
  })
})

describe('ai content notice flag', () => {
  const makeResponse = (data: unknown) => async (config: any) => ({
    data,
    status: 200,
    statusText: 'OK',
    headers: { 'content-type': 'application/json' },
    config
  })

  afterEach(() => {
    apiModule.__setAxiosAdapterForTests(undefined)
  })

  test('/auth-status carries the deployment flag into the store', async () => {
    const { useAiContentNoticeStore } = await import('@/stores/aiContentNotice')
    useAiContentNoticeStore.setState({ enabled: false })

    apiModule.__setAxiosAdapterForTests(
      makeResponse({
        auth_configured: false,
        access_token: 'guest-token',
        ai_content_notice_enabled: true
      })
    )

    await apiModule.getAuthStatus()

    expect(useAiContentNoticeStore.getState().enabled).toBe(true)
  })

  test('/login carries the deployment flag into the store', async () => {
    const { useAiContentNoticeStore } = await import('@/stores/aiContentNotice')
    useAiContentNoticeStore.setState({ enabled: false })

    apiModule.__setAxiosAdapterForTests(
      makeResponse({
        access_token: 'user-token',
        token_type: 'bearer',
        ai_content_notice_enabled: true
      })
    )

    await apiModule.loginToServer('user', 'password')

    expect(useAiContentNoticeStore.getState().enabled).toBe(true)
  })

  test('a server that omits the field does not turn an enabled notice off', async () => {
    const { useAiContentNoticeStore } = await import('@/stores/aiContentNotice')
    useAiContentNoticeStore.setState({ enabled: true })

    apiModule.__setAxiosAdapterForTests(
      makeResponse({ auth_configured: false, access_token: 'guest-token' })
    )

    await apiModule.getAuthStatus()

    expect(useAiContentNoticeStore.getState().enabled).toBe(true)
  })
})
