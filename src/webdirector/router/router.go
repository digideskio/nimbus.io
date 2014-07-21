package router

import (
	"fmt"
	"net/http"
	"os"
	"strings"

	"webdirector/avail"
	"webdirector/hosts"
	"webdirector/mgmtapi"
)

type routerErrorImpl struct {
	httpCode     int
	errorMessage string
}

type routerImpl struct {
	managmentAPIDests  mgmtapi.ManagementAPIDestinations
	hostsForCollection hosts.HostsForCollection
	availability       avail.Availability
}

var (
	serviceDomain string
	destPortMap   map[string]string
)

func init() {
	serviceDomain = os.Getenv("NIMBUS_IO_SERVICE_DOMAIN")

	readDestPort := os.Getenv("NIMBUSIO_WEB_PUBLIC_READER_PORT")
	writeDestPort := os.Getenv("NIMBUSIO_WEB_WRITER_PORT")
	destPortMap = map[string]string{
		"POST":   writeDestPort,
		"DELETE": writeDestPort,
		"PUT":    writeDestPort,
		"PATCH":  writeDestPort,
		"HEAD":   readDestPort,
		"GET":    readDestPort}
}

// NewRouter returns an entity that implements the Router interface
func NewRouter(managmentAPIDests mgmtapi.ManagementAPIDestinations,
	hostsForCollection hosts.HostsForCollection,
	availability avail.Availability) Router {

	return &routerImpl{managmentAPIDests: managmentAPIDests,
		hostsForCollection: hostsForCollection, availability: availability}
}

// Route reads a request and decides where it should go <host:port>
func (router *routerImpl) Route(req *http.Request) (string, error) {

	// TODO: be able to handle http requests from http 1.0 clients w/o a
	// host header to at least the website, if nothing else.
	hostName, ok := req.Header["HOST"]
	if !ok {
		return "", routerErrorImpl{httpCode: http.StatusBadRequest,
			errorMessage: "HOST header not found"}
	}
	routingHostName := strings.Split(hostName[0], ":")[0]
	if !strings.HasSuffix(routingHostName, serviceDomain) {
		return "", routerErrorImpl{httpCode: http.StatusNotFound,
			errorMessage: fmt.Sprintf("Invalid HOST '%s'", routingHostName)}
	}

	if routingHostName == serviceDomain {
		// this is not a request specific to any particular collection
		// TODO: figure out how to route these requests.
		// in production, this might not matter.
		return router.managmentAPIDests.Next(), nil
	}

	destPort, ok := destPortMap[req.Method]
	if !ok {
		return "", routerErrorImpl{httpCode: http.StatusBadRequest,
			errorMessage: fmt.Sprintf("Unknown method '%s'", req.Method)}
	}

	collectionName := parseCollectionFromHostName(routingHostName)
	if collectionName == "" {
		return "", routerErrorImpl{httpCode: http.StatusNotFound,
			errorMessage: fmt.Sprintf("Unparseable host name '%s'", hostName)}
	}

	hostsForCollection, err := router.hostsForCollection.GetHostNames(collectionName)
	if err != nil {
		return "", routerErrorImpl{httpCode: http.StatusNotFound,
			errorMessage: fmt.Sprintf("no hosts for collection '%s'", collectionName)}
	}

	availableHosts, err := router.availability.AvailableHosts(
		hostsForCollection, destPort)
	if err != nil {
		return "", routerErrorImpl{httpCode: http.StatusInternalServerError,
			errorMessage: fmt.Sprintf("collection '%s': %s", collectionName, err)}
	}
	if len(availableHosts) == 0 {
		// XXX: the python web_director retries here, after a delay.
		// IMO, that's what HTTP Status 503 is for
		return "", routerErrorImpl{httpCode: http.StatusServiceUnavailable,
			errorMessage: fmt.Sprintf("no hosts available for collection '%s'",
				collectionName)}
	}

	return "", nil
}

func (err routerErrorImpl) Error() string {
	return fmt.Sprintf("Router Error (%d) %s", err.httpCode, err.errorMessage)
}

func (err routerErrorImpl) HTTPCode() int {
	return err.httpCode
}

func (err routerErrorImpl) ErrorMessage() string {
	return err.errorMessage
}