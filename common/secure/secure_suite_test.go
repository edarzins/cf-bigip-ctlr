/*
 * Portions Copyright (c) 2017, F5 Networks, Inc.
 */

package secure_test

import (
	"testing"

	. "github.com/onsi/ginkgo"
	. "github.com/onsi/gomega"
)

func TestCrypto(t *testing.T) {
	RegisterFailHandler(Fail)
	RunSpecs(t, "Crypto Suite")
}
