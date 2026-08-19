package main

import (
	"fmt"
	"os"
	"time"

	g "github.com/gosnmp/gosnmp"
)

func main() {
	target, community, root := os.Args[1], os.Args[2], os.Args[3]
	c := &g.GoSNMP{
		Target: target, Port: 161, Community: community,
		Version: g.Version2c, Timeout: 10 * time.Second, Retries: 1,
		MaxRepetitions: 25,
	}
	if err := c.Connect(); err != nil {
		fmt.Println("connect:", err)
		return
	}
	defer c.Conn.Close()

	started := time.Now()
	n := 0
	byCol := map[string]int{}
	err := c.BulkWalk(root, func(p g.SnmpPDU) error {
		n++
		// column = the OID element right after the root
		rest := p.Name[len(root):]
		col := rest
		if len(rest) > 1 {
			col = rest[1:]
			for i := 1; i < len(col); i++ {
				if col[i] == '.' {
					col = col[:i]
					break
				}
			}
		}
		byCol[col]++
		return nil
	})
	fmt.Printf("root=%s varbinds=%d elapsed=%s err=%v\n", root, n,
		time.Since(started).Round(time.Millisecond), err)
	for col, count := range byCol {
		fmt.Printf("  .%s -> %d rows\n", col, count)
	}
}
