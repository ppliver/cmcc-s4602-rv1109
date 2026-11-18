package core

import (
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// recyclableWriter emulates a net/http response writer: after the handler
// returns (i.e. WriteTo unblocks), net/http puts the response bufio.Writer
// back into a pool. Any Write after that moment reproduces the production
// panics (nil deref / slice bounds out of range inside bufio).
type recyclableWriter struct {
	first    sync.Once
	ready    chan struct{}
	recycled atomic.Bool
	violated atomic.Bool
}

func (w *recyclableWriter) Write(p []byte) (int, error) {
	w.first.Do(func() { close(w.ready) })
	if w.recycled.Load() {
		w.violated.Store(true)
	}
	return len(p), nil
}

func TestWriteBufferCloseBlocksLateWrites(t *testing.T) {
	wb := NewWriteBuffer(nil)
	rw := &recyclableWriter{ready: make(chan struct{})}

	// init segment is buffered before WriteTo, Reset will copy it into rw
	_, _ = wb.Write([]byte("init"))

	handlerDone := make(chan struct{})
	go func() {
		_, _ = wb.WriteTo(rw) // handlerMP4: blocks until Close
		rw.recycled.Store(true)
		close(handlerDone)
	}()
	<-rw.ready // WriteTo has been called, Writer is switched to rw

	// track sender goroutines keep pushing frames
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := 0; j < 100000; j++ {
				if _, err := wb.Write([]byte("frame")); err != nil {
					return
				}
			}
		}()
	}

	_ = wb.Close() // client disconnected -> cons.Stop() -> Transport.Close()
	<-handlerDone
	wg.Wait()

	if rw.violated.Load() {
		t.Fatal("Write after handler return: production panic scenario reproduced")
	}
	if _, err := wb.Write([]byte("late")); err == nil {
		t.Fatal("Write after Close must return error")
	}
}

// panicFlusher emulates a recycled net/http ResponseWriter: its bufio.Writer
// has been returned to the pool, so Flush() dereferences freed memory and
// panics. This is exactly the S4602 production crash at writebuffer.go:37.
type panicFlusher struct{}

func (w *panicFlusher) Write(p []byte) (int, error) { return len(p), nil }
func (w *panicFlusher) Flush()                      { panic("recycled ResponseWriter") }

// TestWriteBufferFlushPanicRecovered verifies the enhanced patch recovers the
// Flush() panic instead of letting it kill the process. Without the defer
// recover() in Write(), this test crashes the whole test binary.
func TestWriteBufferFlushPanicRecovered(t *testing.T) {
	wb := NewWriteBuffer(&panicFlusher{})

	// Write must not panic; it should return an error after recovering.
	func() {
		defer func() {
			if r := recover(); r != nil {
				t.Fatalf("Write leaked panic to caller: %v", r)
			}
		}()
		if _, err := wb.Write([]byte("frame")); err == nil {
			t.Fatal("Write with panicking Flush must return error after recover")
		}
	}()

	// After a recovered panic the buffer is marked closed: later writes bail
	// out early without touching the panicking Flusher again.
	if _, err := wb.Write([]byte("late")); err == nil {
		t.Fatal("Write after recovered panic must return error")
	}
}

// TestWriteBufferPanicReleasesLock verifies the mutex is released after a
// recovered panic. Without moving Unlock into defer, a panic would skip it
// and any subsequent Write (or Close) would deadlock forever.
func TestWriteBufferPanicReleasesLock(t *testing.T) {
	wb := NewWriteBuffer(&panicFlusher{})
	_, _ = wb.Write([]byte("frame")) // panics inside Flush, recovered

	done := make(chan struct{})
	go func() {
		_, _ = wb.Write([]byte("next")) // must return, not deadlock
		_ = wb.Close()                  // must return, not deadlock
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("mutex not released after recovered Flush panic (deadlock)")
	}
}
